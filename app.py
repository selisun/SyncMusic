#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Navidrome → 飞牛音乐 歌单导入小工具
====================================
纯 Python 标准库实现（http.server + urllib），无需 pip 安装任何依赖。

运行：python app.py   然后浏览器打开 http://127.0.0.1:8787

原理：
  1. 通过 Navidrome 的 Subsonic API（getPlaylists / getPlaylist）拉取歌单和歌曲；
  2. 通过飞牛音乐接口（/music/api/v1）登录、拉取曲库；
  3. 按「标题 + 歌手」模糊匹配，把匹配到的歌曲写入飞牛音乐歌单。
"""

import base64
import difflib
import hashlib
import hmac
import http.server
import json
import os
import random
import re
import socketserver
import ssl
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser

APP_DIR = os.path.dirname(os.path.abspath(__file__))
# 数据目录：默认与程序同目录；fpk 应用模式通过 --data-dir 指定（包用户只能写数据目录）
DATA_DIR = APP_DIR
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
WEB_DIR = os.path.join(APP_DIR, "web")
# web 目录内允许直接访问的静态资源类型（赞赏码等图片）
WEB_ASSETS = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".svg": "image/svg+xml", ".ico": "image/x-icon",
}
# 统一网关前缀（如 /app/SyncMusic）：fnOS 转发请求时不会去掉前缀，需要自行剥离；PC 版为空
BASE_PATH = ""

DEFAULT_CONFIG = {
    "navidrome": {"url": "", "user": "", "password": ""},
    "fnos": {"url": "", "user": "", "password": ""},
    "deviceId": uuid.uuid4().hex,
    "advanced": {
        "port": 8787,
        "useAuthx": False,
        "authxPrefix": "NDzZTVxnRKP8Z0jXg1VAMonaG8akvh",
        "clientKey": "zIGtkc3dqZnJpd29qZXJqa2w7c",
        "skipTlsVerify": False,
    },
}

CONFIG_LOCK = threading.Lock()
SESSION_LOCK = threading.Lock()
# 飞牛音乐登录会话缓存：key=(url,user,passhash) -> FnOsClient
FNOS_SESSIONS = {}
# 飞牛曲库缓存：key=(url,user,passhash) -> list[track]
FNOS_LIBRARY = {}


class ToolError(Exception):
    """给用户看的业务错误。"""


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------

SECRET_PREFIX = "enc:v1:"
_SECRET_KEY_CACHE = []


def _machine_id():
    """取本机稳定标识，用于派生配置文件的加密密钥。"""
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            with open(path, "r", encoding="utf-8") as f:
                value = f.read().strip()
            if value:
                return value
        except OSError:
            pass
    return "%s|%s" % (os.environ.get("COMPUTERNAME") or "", uuid.getnode())


def _secret_key():
    if not _SECRET_KEY_CACHE:
        raw = "SyncMusic|secret|v1|" + _machine_id()
        _SECRET_KEY_CACHE.append(hashlib.sha256(raw.encode("utf-8")).digest())
    return _SECRET_KEY_CACHE[0]


def _keystream(nonce, size):
    """以 SHA-256 计数器模式生成密钥流（纯标准库，替代 AES）。"""
    key = _secret_key()
    out = bytearray()
    counter = 0
    while len(out) < size:
        out += hashlib.sha256(key + nonce + counter.to_bytes(4, "big")).digest()
        counter += 1
    return bytes(out[:size])


def encrypt_secret(text):
    """密码落盘前加密：enc:v1:base64(nonce + 密文 + HMAC)。"""
    if not text:
        return ""
    nonce = os.urandom(16)
    data = text.encode("utf-8")
    cipher = bytes(a ^ b for a, b in zip(data, _keystream(nonce, len(data))))
    tag = hmac.new(_secret_key(), nonce + cipher, hashlib.sha256).digest()[:16]
    return SECRET_PREFIX + base64.b64encode(nonce + cipher + tag).decode("ascii")


def decrypt_secret(saved):
    """读取配置里的密码；旧版明文配置原样返回（下次保存时自动转为密文）。"""
    if not saved:
        return ""
    if not saved.startswith(SECRET_PREFIX):
        return saved
    try:
        raw = base64.b64decode(saved[len(SECRET_PREFIX):])
        if len(raw) < 32:
            return ""
        nonce, cipher, tag = raw[:16], raw[16:-16], raw[-16:]
        want = hmac.new(_secret_key(), nonce + cipher, hashlib.sha256).digest()[:16]
        if not hmac.compare_digest(tag, want):
            raise ValueError("校验失败")
        data = bytes(a ^ b for a, b in zip(cipher, _keystream(nonce, len(cipher))))
        return data.decode("utf-8")
    except Exception as e:
        # 换机器或手改过配置时会失败：当作未保存，让用户重新输入
        print("[config] 已保存的密码无法解密:", e)
        return ""


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    plain_found = False
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
            for sec in ("navidrome", "fnos"):
                if isinstance(saved.get(sec), dict):
                    sec_saved = saved[sec]
                    cfg[sec].update({k: v for k, v in sec_saved.items()
                                     if k in cfg[sec] and k != "password"})
                    raw = sec_saved.get("password") or ""
                    cfg[sec]["password"] = decrypt_secret(raw)
                    if raw and not raw.startswith(SECRET_PREFIX):
                        plain_found = True
            if isinstance(saved.get("advanced"), dict):
                cfg["advanced"].update({k: v for k, v in saved["advanced"].items() if k in cfg["advanced"]})
            if saved.get("deviceId"):
                cfg["deviceId"] = saved["deviceId"]
        except Exception as e:
            print("[config] 读取失败，使用默认配置:", e)
    if plain_found:
        # 旧版明文密码：立即迁移为密文
        try:
            save_config(cfg)
        except OSError as e:
            print("[config] 明文密码迁移失败:", e)
    return cfg


def save_config(cfg):
    """密码统一加密后落盘，任何时候都不写明文。"""
    disk = json.loads(json.dumps(cfg))
    for sec in ("navidrome", "fnos"):
        disk[sec]["password"] = encrypt_secret(cfg[sec].get("password") or "")
    with CONFIG_LOCK:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(disk, f, ensure_ascii=False, indent=2)



def config_public(cfg):
    """返回给前端的配置：密码不回传，只告知是否已保存。"""
    out = {
        "navidrome": {
            "url": cfg["navidrome"]["url"],
            "user": cfg["navidrome"]["user"],
            "hasPassword": bool(cfg["navidrome"]["password"]),
        },
        "fnos": {
            "url": cfg["fnos"]["url"],
            "user": cfg["fnos"]["user"],
            "hasPassword": bool(cfg["fnos"]["password"]),
        },
        "advanced": cfg["advanced"],
    }
    return out


def merge_config(cfg, payload):
    """前端提交的配置合并进内存配置。密码留空 → 沿用已保存（加密）的密码。"""
    for sec in ("navidrome", "fnos"):
        if sec not in (payload or {}):
            continue
        data = payload.get(sec) or {}
        cfg[sec]["url"] = (data.get("url") or "").strip()
        cfg[sec]["user"] = (data.get("user") or "").strip()
        if data.get("password"):
            cfg[sec]["password"] = data["password"]
    adv = payload.get("advanced") or {}
    for k in ("useAuthx", "skipTlsVerify"):
        if k in adv:
            cfg["advanced"][k] = bool(adv[k])
    for k in ("authxPrefix", "clientKey"):
        if adv.get(k):
            cfg["advanced"][k] = str(adv[k]).strip()
    try:
        cfg["advanced"]["port"] = int(adv.get("port") or cfg["advanced"]["port"])
    except (TypeError, ValueError):
        pass
    return cfg


# --------------------------------------------------------------------------
# 通用 HTTP 工具
# --------------------------------------------------------------------------

def make_ssl_context(skip_verify):
    if skip_verify:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return None


def http_json(url, method="GET", headers=None, body=None, timeout=20, ssl_ctx=None):
    """发起请求并解析 JSON，返回 (status, obj)。非 JSON 响应时 obj=None。"""
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_ctx) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        status = e.code
    except urllib.error.URLError as e:
        raise ToolError("无法连接服务器：%s" % getattr(e, "reason", e))
    except (ssl.SSLError, ConnectionError, http.client.HTTPException) as e:
        raise ToolError("网络错误：%s" % e)
    except TimeoutError:
        raise ToolError("连接超时")
    obj = None
    if raw:
        try:
            obj = json.loads(raw)
        except ValueError:
            obj = None
    return status, obj


# --------------------------------------------------------------------------
# Navidrome（Subsonic API）
# --------------------------------------------------------------------------

def _as_list(v):
    """Subsonic 在只有一个元素时返回对象而非数组，这里统一成列表。"""
    if isinstance(v, list):
        return v
    if isinstance(v, dict):
        return [v]
    return []


class SubsonicClient:
    def __init__(self, base_url, user, password, skip_tls=False):
        if not base_url:
            raise ToolError("请先填写 Navidrome 地址")
        self.base = base_url.rstrip("/")
        self.user = user
        self.password = password
        self.ssl_ctx = make_ssl_context(skip_tls)

    def _get(self, path, extra=None):
        salt = uuid.uuid4().hex[:12]
        token = hashlib.md5((self.password + salt).encode("utf-8")).hexdigest()
        params = {
            "u": self.user, "t": token, "s": salt,
            "v": "1.16.1", "c": "syncmusic", "f": "json",
        }
        if extra:
            params.update(extra)
        url = "%s%s?%s" % (self.base, path, urllib.parse.urlencode(params))
        status, obj = http_json(url, ssl_ctx=self.ssl_ctx)
        if obj is None:
            raise ToolError("Navidrome 响应异常（HTTP %s），请检查地址是否正确" % status)
        resp = obj.get("subsonic-response") or {}
        if resp.get("status") != "ok":
            err = (resp.get("error") or {}).get("message", "未知错误")
            code = (resp.get("error") or {}).get("code")
            if code == 40:
                raise ToolError("Navidrome 用户名或密码错误（%s）" % err)
            raise ToolError("Navidrome 接口错误：%s" % err)
        return resp

    def ping(self):
        self._get("/rest/ping")
        return True

    def get_playlists(self):
        resp = self._get("/rest/getPlaylists")
        playlists = ((resp.get("playlists") or {}).get("playlist")) or []
        out = []
        for p in playlists:
            out.append({
                "id": str(p.get("id", "")),
                "name": p.get("name", ""),
                "songCount": int(p.get("songCount") or 0),
                "duration": int(p.get("duration") or 0),
                "owner": p.get("owner", ""),
                "public": bool(p.get("public")),
            })
        return out

    def get_playlist_songs(self, playlist_id):
        resp = self._get("/rest/getPlaylist", {"id": playlist_id})
        pl = resp.get("playlist") or {}
        entries = pl.get("entry") or []
        songs = []
        for e in entries:
            songs.append({
                "id": str(e.get("id", "")),
                "title": e.get("title", ""),
                "artist": e.get("artist", ""),
                "album": e.get("album", ""),
                "path": e.get("path", ""),
                "duration": int(e.get("duration") or 0),
                "track": int(e.get("track") or 0),
            })
        return {"name": pl.get("name", ""), "songs": songs}

    def get_starred_songs(self):
        """Navidrome 收藏：歌曲 + 收藏专辑/艺人下的曲目（自动去重，标注来源）。"""
        resp = self._get("/rest/getStarred2")
        starred = resp.get("starred2") or {}
        songs = []
        seen = set()

        def add(e, source):
            sid = str(e.get("id", ""))
            if not sid or sid in seen:
                return
            seen.add(sid)
            songs.append({
                "id": sid,
                "title": e.get("title", ""),
                "artist": e.get("artist", ""),
                "album": e.get("album", ""),
                "path": e.get("path", ""),
                "duration": int(e.get("duration") or 0),
                "track": int(e.get("track") or 0),
                "source": source,
            })

        for e in _as_list(starred.get("song")):
            add(e, "收藏歌曲")
        for al in _as_list(starred.get("album")):
            detail = self._get("/rest/getAlbum", {"id": al.get("id")}).get("album") or {}
            for e in _as_list(detail.get("song")):
                add(e, "专辑《%s》" % (al.get("name") or ""))
        for ar in _as_list(starred.get("artist")):
            detail = self._get("/rest/getArtist", {"id": ar.get("id")}).get("artist") or {}
            for alb in _as_list(detail.get("album")):
                alb_detail = self._get("/rest/getAlbum", {"id": alb.get("id")}).get("album") or {}
                for e in _as_list(alb_detail.get("song")):
                    add(e, "艺人 %s" % (ar.get("name") or ""))
        return songs


# --------------------------------------------------------------------------
# 飞牛音乐（/music/api/v1）
# --------------------------------------------------------------------------

AUTHX_DEFAULT_PREFIX = DEFAULT_CONFIG["advanced"]["authxPrefix"]
AUTHX_DEFAULT_KEY = DEFAULT_CONFIG["advanced"]["clientKey"]


def compute_authx(method, path, query_str, body_str, prefix, client_key):
    """authx 签名（与飞牛音乐官方前端一致，直接访问 NAS 时通常不需要）。"""
    payload = ""
    if method == "GET":
        if query_str:
            # 与官方前端一致：参数按 key 排序、值做 URL 编码
            pairs = sorted(urllib.parse.parse_qsl(query_str, keep_blank_values=True))
            payload = "&".join("%s=%s" % (k, urllib.parse.quote(str(v), safe="")) for k, v in pairs)
    else:
        payload = body_str or ""
    nonce = str(random.randint(100000, 999999))
    ts = str(int(time.time() * 1000))
    h = lambda s: hashlib.md5(s.encode("utf-8")).hexdigest()
    sign = h("_".join([prefix, path, nonce, ts, h(payload), client_key]))
    return "nonce=%s&timestamp=%s&sign=%s" % (nonce, ts, sign)


def _shape_of(o):
    """仅展示 JSON 结构（字段名/类型），不含敏感值"""
    if isinstance(o, dict):
        return "{" + ",".join(list(o.keys())[:20]) + "}"
    if isinstance(o, list):
        return "列表[%d项]" % len(o)
    if o is None:
        return "null"
    if isinstance(o, str):
        return "字符串(len=%d)" % len(o)
    return type(o).__name__


class FnOsClient:
    def __init__(self, base_url, user, password, device_id,
                 use_authx=False, authx_prefix="", client_key="", skip_tls=False):
        if not base_url:
            raise ToolError("请先填写飞牛音乐地址")
        self.root = base_url.rstrip("/")
        low = self.root.lower()
        # 兼容三种填法：NAS 根地址 / 带 /music 的应用地址 / 完整 API 地址
        if low.endswith("/music/api/v1"):
            self.api = self.root
        elif low.endswith("/music"):
            self.api = self.root + "/api/v1"
        else:
            self.api = self.root + "/music/api/v1"
        self.user = user
        self.password = password
        self.device_id = device_id
        self.use_authx = use_authx
        self.authx_prefix = authx_prefix or AUTHX_DEFAULT_PREFIX
        self.client_key = client_key or AUTHX_DEFAULT_KEY
        self.ssl_ctx = make_ssl_context(skip_tls)
        self.token = None

    # ---- 底层请求 ----
    def _request(self, method, path, query=None, body=None, _retry=True, raw=False):
        url = self.api + path
        query_str = ""
        if query:
            query_str = urllib.parse.urlencode(query)
            url += "?" + query_str
        body_str = ""
        body_bytes = None
        if body is not None:
            body_str = json.dumps(body, ensure_ascii=False)
            body_bytes = body_str.encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "syncmusic/1.0",
        }
        if self.token:
            headers["Cookie"] = "music-token=" + self.token
        if self.use_authx:
            headers["authx"] = compute_authx(
                method, path, query_str, body_str, self.authx_prefix, self.client_key)
        status, obj = http_json(url, method=method, headers=headers,
                                body=body_bytes, ssl_ctx=self.ssl_ctx)
        if obj is None:
            if status == 401:
                raise ToolError("登录状态已失效，请重新测试连接")
            raise ToolError("飞牛音乐响应异常（HTTP %s），请检查地址（应为 NAS 地址，如 192.168.1.10:5666）" % status)
        code = obj.get("code")
        if code != 0:
            msg = obj.get("msg") or "错误码 %s" % code
            if code == 120001:
                if _retry and self.token:
                    # token 过期：重新登录一次再试
                    self.token = None
                    self.login()
                    return self._request(method, path, query, body, _retry=False)
                raise ToolError("登录状态已失效（%s）" % msg)
            if status == 401 and _retry and self.token:
                self.token = None
                self.login()
                return self._request(method, path, query, body, _retry=False)
            raise ToolError("飞牛音乐接口错误：%s" % msg)
        if raw:
            return obj
        return obj.get("data")

    # ---- 登录与基础 ----
    def login(self):
        hashed = hashlib.sha256(self.password.encode("utf-8")).hexdigest()
        obj = self._request("POST", "/user/password-login", body={
            "username": self.user,
            "password": hashed,
            "deviceId": self.device_id,
        }, _retry=False, raw=True)
        data = obj.get("data") if isinstance(obj, dict) else None
        # 兼容不同版本：token 可能位于 data 或顶层，字段名可能不同
        token = None
        for src in ((data or {}), (obj or {})):
            if isinstance(src, dict):
                for key in ("userToken", "token", "accessToken", "musicToken", "music_token", "jwt"):
                    v = src.get(key)
                    if isinstance(v, str) and v:
                        token = v
                        break
            if token:
                break
        if not token:
            raise ToolError(
                "登录接口已连通（code=%s），但未找到 token。顶层字段：%s；data：%s。请把此提示截图反馈给开发者"
                % (obj.get("code") if isinstance(obj, dict) else "?",
                   _shape_of(obj), _shape_of(data)))
        self.token = token
        return True

    def library_stats(self):
        data = self._request("GET", "/track/list", query={"page": 1, "pageSize": 1})
        total = int((data or {}).get("total") or 0)
        pl = self._request("GET", "/playlist/list") or {}
        pl_total = int(pl.get("total") or len(pl.get("list") or []))
        return {"trackTotal": total, "playlistTotal": pl_total}

    # ---- 曲库 ----
    def all_tracks(self, page_size=100):
        tracks = []
        page = 1
        total = None
        while True:
            data = self._request("GET", "/track/list",
                                 query={"page": page, "pageSize": page_size})
            data = data or {}
            total = int(data.get("total") or (total or 0))
            batch = data.get("list") or []
            tracks.extend(batch)
            if not batch or len(tracks) >= total:
                break
            page += 1
        return tracks

    # ---- 歌单 ----
    def playlist_list(self):
        data = self._request("GET", "/playlist/list") or {}
        out = []
        for p in (data.get("list") or []):
            out.append({
                "guid": p.get("guid", ""),
                "name": p.get("name", ""),
                "trackCount": int(p.get("trackCount") or p.get("songCount") or 0),
            })
        return out

    def playlist_create(self, name):
        data = self._request("POST", "/playlist/create", body={"name": name})
        guid = (data or {}).get("guid") if isinstance(data, dict) else None
        if not guid:
            raise ToolError("创建歌单「%s」失败：未返回 guid" % name)
        return guid

    def playlist_delete(self, guid):
        self._request("POST", "/playlist/delete", body={"guid": guid})

    def playlist_edit(self, guid, name):
        self._request("POST", "/playlist/edit", body={"guid": guid, "name": name})

    def playlist_tracks(self, guid, page_size=300):
        """歌单内曲目列表（分页拉全），用于判断哪些歌已在目标歌单里。"""
        tracks = []
        page = 1
        total = None
        while True:
            data = self._request("GET", "/track/playlist-detail/list",
                                 query={"playlistGUID": guid, "page": page,
                                        "pageSize": page_size}) or {}
            total = int(data.get("total") or (total or 0))
            batch = data.get("list") or []
            tracks.extend(batch)
            if not batch or len(tracks) >= total:
                break
            page += 1
        return tracks

    def playlist_add_tracks(self, guid, track_guids, chunk=100):
        added = 0
        for i in range(0, len(track_guids), chunk):
            part = track_guids[i:i + chunk]
            self._request("POST", "/playlist/add-track",
                          body={"guid": guid, "trackGUIDs": part})
            added += len(part)
        return added

    # ---- 收藏（曲目级） ----
    def favorite_tracks(self):
        """已收藏曲目的 guid 集合。"""
        guids = set()
        page = 1
        total = None
        while True:
            data = self._request("GET", "/favorite-track/list",
                                 query={"page": page, "size": 300}) or {}
            batch = data.get("list") or []
            total = int(data.get("total") or (total or 0))
            for it in batch:
                g = it.get("guid") or ""
                if not g and isinstance(it.get("track"), dict):
                    g = it["track"].get("guid") or ""
                if g:
                    guids.add(g)
            if not batch or len(guids) >= total:
                break
            page += 1
        return guids

    def favorite_add(self, track_guid):
        self._request("POST", "/favorite-track/create", body={"trackGUID": track_guid})


# --------------------------------------------------------------------------
# 歌曲匹配
# --------------------------------------------------------------------------

_PUNCT_RE = re.compile(r"[\W_]+", re.UNICODE)


def norm_text(s):
    """归一化：全半角统一、去空格标点、小写（保留中文字符）。"""
    s = unicodedata.normalize("NFKC", s or "")
    s = _PUNCT_RE.sub("", s).casefold()
    return s


def fnos_track_info(t):
    """从飞牛曲目对象取 (title, artist_names, album)。字段名做兼容处理。"""
    title = t.get("title") or t.get("name") or ""
    artists = t.get("artists") or []
    names = []
    for a in artists:
        if isinstance(a, dict):
            names.append(a.get("name") or "")
        elif isinstance(a, str):
            names.append(a)
    album = t.get("album")
    if isinstance(album, dict):
        album = album.get("name") or ""
    return title, [n for n in names if n], album or ""


def match_score(nav_song, fn_track):
    """返回 0~1 匹配分：标题权重 0.8，歌手权重 0.2。"""
    fn_title, fn_artists, _fn_album = fnos_track_info(fn_track)
    t1, t2 = norm_text(nav_song.get("title")), norm_text(fn_title)
    if not t1 or not t2:
        return 0.0
    if t1 == t2:
        title_s = 1.0
    else:
        title_s = difflib.SequenceMatcher(None, t1, t2).ratio()
    a1 = norm_text(nav_song.get("artist"))
    artist_s = 0.0
    if a1 and fn_artists:
        fn_norm = [norm_text(x) for x in fn_artists]
        if a1 in fn_norm:
            artist_s = 1.0
        else:
            joined = norm_text(" ".join(fn_artists))
            if a1 in joined or joined in a1:
                artist_s = 0.8
            else:
                artist_s = max((difflib.SequenceMatcher(None, a1, x).ratio() for x in fn_norm),
                               default=0.0)
    elif not a1 or not fn_artists:
        artist_s = 0.5  # 一方缺歌手信息：不奖不罚
    return title_s * 0.8 + artist_s * 0.2


class LibraryIndex:
    """飞牛曲库索引：归一化标题 -> 候选列表。"""

    def __init__(self, tracks):
        self.tracks = tracks
        self.by_title = {}
        for t in tracks:
            title, _a, _al = fnos_track_info(t)
            key = norm_text(title)
            if key:
                self.by_title.setdefault(key, []).append(t)

    def best_match(self, nav_song):
        key = norm_text(nav_song.get("title"))
        candidates = self.by_title.get(key, [])
        if not candidates:
            # 标题完全对不上时，做一次宽松的模糊候选（相似度 > 0.85）
            close = difflib.get_close_matches(key, self.by_title.keys(), n=3, cutoff=0.85)
            for k in close:
                candidates.extend(self.by_title[k])
        best, best_score = None, 0.0
        for c in candidates:
            s = match_score(nav_song, c)
            if s > best_score:
                best, best_score = c, s
        if best is None and self.tracks:
            # 兜底：小曲库时全量比一遍（大曲库靠上面的标题索引已足够快）
            if len(self.tracks) <= 3000:
                for c in self.tracks:
                    s = match_score(nav_song, c)
                    if s > best_score:
                        best, best_score = c, s
        return best, round(best_score, 3)

    def match_songs(self, songs):
        rows = []
        for s in songs:
            track, score = self.best_match(s)
            fn_title, fn_artists, _ = fnos_track_info(track) if track else ("", [], "")
            rows.append({
                "navTitle": s.get("title", ""),
                "navArtist": s.get("artist", ""),
                "navAlbum": s.get("album", ""),
                "navPath": s.get("path", ""),
                "navDuration": s.get("duration", 0),
                "matched": bool(track),
                "score": score,
                "level": "high" if score >= 0.85 else ("mid" if score >= 0.6 else "none"),
                "fnGuid": (track or {}).get("guid", ""),
                "fnTitle": fn_title,
                "fnArtist": " / ".join(fn_artists),
            })
        return rows


# --------------------------------------------------------------------------
# 会话管理（登录缓存 + 曲库缓存）
# --------------------------------------------------------------------------

def get_fnos_session(cfg):
    fn = cfg["fnos"]
    if not fn["url"] or not fn["user"] or not fn["password"]:
        raise ToolError("请先在「连接设置」中填写并保存飞牛音乐地址、账号、密码")
    adv = cfg["advanced"]
    key = (fn["url"], fn["user"], hashlib.sha256(fn["password"].encode("utf-8")).hexdigest())
    with SESSION_LOCK:
        client = FNOS_SESSIONS.get(key)
        if client is None:
            client = FnOsClient(fn["url"], fn["user"], fn["password"], cfg["deviceId"],
                                use_authx=adv["useAuthx"], authx_prefix=adv["authxPrefix"],
                                client_key=adv["clientKey"], skip_tls=adv["skipTlsVerify"])
            client.login()
            FNOS_SESSIONS.clear()  # 只保留最新一个会话，避免旧地址占内存
            FNOS_SESSIONS[key] = client
        return client


def get_fnos_library(cfg, client):
    key = (client.root, client.user, hashlib.sha256(client.password.encode("utf-8")).hexdigest())
    with SESSION_LOCK:
        lib = FNOS_LIBRARY.get(key)
        if lib is None:
            lib = client.all_tracks()
            FNOS_LIBRARY.clear()
            FNOS_LIBRARY[key] = lib
        return lib


def invalidate_fnos_cache():
    with SESSION_LOCK:
        FNOS_SESSIONS.clear()
        FNOS_LIBRARY.clear()


def get_subsonic(cfg):
    nd = cfg["navidrome"]
    if not nd["url"] or not nd["user"] or not nd["password"]:
        raise ToolError("请先在「连接设置」中填写并保存 Navidrome 地址、账号、密码")
    return SubsonicClient(nd["url"], nd["user"], nd["password"],
                          skip_tls=cfg["advanced"]["skipTlsVerify"])


# --------------------------------------------------------------------------
# Web 服务
# --------------------------------------------------------------------------

def api_config_load(payload):
    cfg = load_config()
    return config_public(cfg)


def api_config_save(payload):
    cfg = load_config()
    cfg = merge_config(cfg, payload)
    # 密码加密落盘，重启后无需重新输入
    save_config(cfg)
    invalidate_fnos_cache()
    return config_public(cfg)


def api_navidrome_test(payload):
    cfg = load_config()
    cli = get_subsonic(cfg)
    playlists = cli.get_playlists()
    return {"ok": True, "playlists": playlists, "count": len(playlists)}


def api_fnos_test(payload):
    cfg = load_config()
    client = get_fnos_session(cfg)
    stats = client.library_stats()
    return {"ok": True, **stats}


def api_preview(payload):
    """拉取指定 Navidrome 歌单并在飞牛曲库中匹配，同时标记已在目标歌单里的歌。"""
    ids = payload.get("ids") or []
    if not ids:
        raise ToolError("请先选择要导入的歌单")
    cfg = load_config()
    sub = get_subsonic(cfg)
    fnos = get_fnos_session(cfg)
    library = get_fnos_library(cfg, fnos)
    index = LibraryIndex(library)
    pl_index = {p["name"]: p for p in fnos.playlist_list()}
    result = []
    for pid in ids:
        pl = sub.get_playlist_songs(pid)
        rows = index.match_songs(pl["songs"])
        target = pl_index.get((pl["name"] or "").strip())
        imported_guids = set()
        if target:
            try:
                imported_guids = {t.get("guid", "") for t in fnos.playlist_tracks(target["guid"])}
            except ToolError:
                imported_guids = set()  # 目标歌单曲目拉取失败时按未导入处理，不阻塞预览
        for r in rows:
            r["imported"] = bool(r["fnGuid"]) and r["fnGuid"] in imported_guids
        result.append({
            "id": pid,
            "name": pl["name"],
            "songCount": len(pl["songs"]),
            "rows": rows,
            "matchedCount": sum(1 for r in rows if r["matched"]),
            "targetExists": bool(target),
            "targetCount": len(imported_guids),
        })
    return {"playlists": result, "librarySize": len(library)}


def api_import(payload):
    """创建/追加飞牛音乐歌单。items=[{name, guids:[...]}]，mode=append|new|overwrite"""
    items = payload.get("items") or []
    mode = payload.get("mode") or "append"
    if not items:
        raise ToolError("没有可导入的内容")
    cfg = load_config()
    fnos = get_fnos_session(cfg)
    existing = {p["name"]: p["guid"] for p in fnos.playlist_list()}
    log = []
    for it in items:
        name = (it.get("name") or "").strip()
        guids = [g for g in (it.get("guids") or []) if g]
        if not name:
            log.append({"name": name, "status": "error", "msg": "歌单名为空，已跳过"})
            continue
        if not guids:
            log.append({"name": name, "status": "error", "msg": "没有匹配到歌曲，已跳过"})
            continue
        try:
            if mode == "append" and name in existing:
                added = fnos.playlist_add_tracks(existing[name], guids)
                log.append({"name": name, "status": "ok", "msg": "已追加 %d 首到现有歌单" % added})
            elif mode == "overwrite" and name in existing:
                fnos.playlist_delete(existing[name])
                guid = fnos.playlist_create(name)
                existing[name] = guid
                fnos.playlist_add_tracks(guid, guids)
                log.append({"name": name, "status": "ok",
                            "msg": "已删除旧歌单并新建，写入 %d 首" % len(guids)})
            else:
                # new 模式：重名时自动加后缀
                final = name
                i = 2
                while final in existing:
                    final = "%s (%d)" % (name, i)
                    i += 1
                guid = fnos.playlist_create(final)
                existing[final] = guid
                fnos.playlist_add_tracks(guid, guids)
                note = "" if final == name else "（重名，已保存为「%s」）" % final
                log.append({"name": name, "status": "ok",
                            "msg": "新建歌单，写入 %d 首%s" % (len(guids), note)})
        except ToolError as e:
            log.append({"name": name, "status": "error", "msg": str(e)})
    ok_count = sum(1 for x in log if x["status"] == "ok")
    return {"log": log, "ok": ok_count, "fail": len(log) - ok_count}


def api_starred(payload):
    """拉取 Navidrome 收藏歌曲并在飞牛曲库中匹配。"""
    cfg = load_config()
    sub = get_subsonic(cfg)
    fnos = get_fnos_session(cfg)
    library = get_fnos_library(cfg, fnos)
    index = LibraryIndex(library)
    songs = sub.get_starred_songs()
    rows = index.match_songs(songs)
    for i, r in enumerate(rows):
        r["source"] = songs[i].get("source", "")
    existing = fnos.favorite_tracks()
    for r in rows:
        r["alreadyFavorite"] = bool(r["fnGuid"]) and r["fnGuid"] in existing
    return {
        "rows": rows,
        "librarySize": len(library),
        "total": len(songs),
        "matched": sum(1 for r in rows if r["matched"]),
        "already": sum(1 for r in rows if r["alreadyFavorite"]),
    }


def api_favorite_import(payload):
    """把匹配到的曲目标记为飞牛音乐收藏。guids=[...]"""
    guids = []
    seen = set()
    for g in (payload.get("guids") or []):
        if g and g not in seen:
            seen.add(g)
            guids.append(g)
    if not guids:
        raise ToolError("没有可导入的收藏曲目")
    cfg = load_config()
    fnos = get_fnos_session(cfg)
    existing = fnos.favorite_tracks()
    added = skipped = 0
    errors = []
    for g in guids:
        if g in existing:
            skipped += 1
            continue
        try:
            fnos.favorite_add(g)
            added += 1
        except ToolError as e:
            errors.append("%s：%s" % (g[:8], e))
    return {"added": added, "skipped": skipped, "fail": len(errors), "errors": errors[:5]}


def api_logout(payload):
    invalidate_fnos_cache()
    return {"ok": True}


ROUTES = {
    "/api/config/load": api_config_load,
    "/api/config/save": api_config_save,
    "/api/navidrome/test": api_navidrome_test,
    "/api/fnos/test": api_fnos_test,
    "/api/preview": api_preview,
    "/api/starred": api_starred,
    "/api/favorite/import": api_favorite_import,
    "/api/import": api_import,
    "/api/logout": api_logout,
}

# 会修改连接配置或写入飞牛音乐的接口：网关模式下仅管理员可用
ADMIN_ROUTES = ("/api/config/save", "/api/favorite/import", "/api/import")


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "syncmusic/1.0"

    # ---- 输出工具 ----
    def _send_json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, path, content_type):
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            self._send_json({"ok": False, "error": "文件不存在"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):  # 静默默认访问日志
        pass

    # ---- 路由 ----
    def _strip_base(self, raw_path):
        """剥掉统一网关前缀：网关转发到应用时不会去掉 /app/{appname}。"""
        path = urllib.parse.urlparse(raw_path).path
        if not BASE_PATH:
            return path
        if path == BASE_PATH:
            return "/"
        if path.startswith(BASE_PATH + "/"):
            return path[len(BASE_PATH):]
        return path

    def do_GET(self):
        path = self._strip_base(self.path)
        if path in ("/", "/index.html"):
            self._send_file(os.path.join(WEB_DIR, "index.html"),
                            "text/html; charset=utf-8")
        elif path == "/favicon.ico":
            self._send_json({"ok": True})
        else:
            asset = self._web_asset(path)
            if asset:
                self._send_file(asset[0], asset[1])
            else:
                self._send_json({"ok": False, "error": "Not Found"}, 404)

    def _web_asset(self, path):
        """只放行 web 目录内的图片资源，避免目录穿越。"""
        rel = path.lstrip("/")
        ctype = WEB_ASSETS.get(os.path.splitext(rel)[1].lower())
        if not rel or not ctype:
            return None
        full = os.path.abspath(os.path.join(WEB_DIR, rel))
        root = os.path.abspath(WEB_DIR) + os.sep
        if full.startswith(root) and os.path.isfile(full):
            return full, ctype
        return None

    def do_POST(self):
        path = self._strip_base(self.path)
        func = ROUTES.get(path)
        if func is None:
            self._send_json({"ok": False, "error": "Not Found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            self._send_json({"ok": False, "error": "请求体不是合法 JSON"})
            return
        # 网关会带 X-Trim-Isadmin 转发用户身份；PC 版无该头，保持原有行为
        if path in ADMIN_ROUTES:
            is_admin = self.headers.get("X-Trim-Isadmin")
            if is_admin is not None and is_admin.strip().lower() != "true":
                self._send_json({"ok": False, "error": "仅管理员可执行该操作"}, 403)
                return
        try:
            result = func(payload)
            self._send_json({"ok": True, "data": result})
        except ToolError as e:
            self._send_json({"ok": False, "error": str(e)})
        except Exception as e:
            print("[%s] 内部错误: %r" % (path, e))
            self._send_json({"ok": False, "error": "内部错误：%s" % e})


if hasattr(socketserver, "UnixStreamServer"):
    class ThreadingUnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        """Unix Socket 版 HTTP 服务，供 fnOS 统一网关转发使用。"""
        daemon_threads = True

        def __init__(self, socket_path, handler_cls):
            if os.path.exists(socket_path):
                try:
                    os.unlink(socket_path)
                except OSError:
                    pass
            super().__init__(socket_path, handler_cls)
            self.server_name = "localhost"
            self.server_port = 0
else:  # Windows 等不支持 AF_UNIX 的平台上没有该类型，PC 版走端口即可
    ThreadingUnixHTTPServer = None


class HealthHandler(http.server.BaseHTTPRequestHandler):
    """仅供本机端口探测使用：任何路径都只回一个 ok，不暴露任何 API。

    fpk 模式下真正的对外入口是统一网关 socket；这个 TCP 监听只用于
    应用中心/系统探测进程是否存活，因此不能挂载完整路由。
    """

    def do_GET(self):
        self._ok()

    def do_HEAD(self):
        self._ok()

    def do_POST(self):
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _ok(self):
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


def _parse_args(argv):
    """解析 --port N / --host H / --unix-socket P / --base-path P / --no-browser。"""
    args = {"no_browser": False}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--no-browser":
            args["no_browser"] = True
        elif a in ("--port", "--host", "--data-dir", "--unix-socket", "--base-path") and i + 1 < len(argv):
            args[a[2:]] = argv[i + 1]
            i += 1
        i += 1
    return args


def main():
    global BASE_PATH, DATA_DIR, CONFIG_PATH
    args = _parse_args(sys.argv[1:])
    host = args.get("host") or "127.0.0.1"
    if args.get("data-dir"):
        DATA_DIR = os.path.abspath(args["data-dir"])
        CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
        os.makedirs(DATA_DIR, exist_ok=True)
    if args.get("base-path"):
        BASE_PATH = "/" + str(args["base-path"]).strip("/")
    cfg = load_config()
    try:
        port = int(args.get("port") or cfg["advanced"].get("port") or 8787)
    except ValueError:
        port = 8787

    servers = []
    unix_path = args.get("unix-socket")
    if unix_path and ThreadingUnixHTTPServer is None:
        print("当前系统不支持 Unix Socket，忽略 --unix-socket")
        unix_path = None
    if unix_path:
        parent = os.path.dirname(unix_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        try:
            servers.append(ThreadingUnixHTTPServer(unix_path, Handler))
            print("统一网关 Socket：%s（前缀 %s）" % (unix_path, BASE_PATH or "-"))
        except OSError as e:
            print("Unix Socket 启动失败：%s" % e)
            sys.exit(1)

    tcp = None
    # 有网关 socket 时，TCP 端口只做存活探测，不挂载 API（避免第二个免鉴权入口）
    tcp_handler = HealthHandler if unix_path else Handler
    if args.get("port") or not unix_path:
        if args.get("port"):
            # 指定了固定端口：只试这一个，失败直接退出
            try:
                tcp = http.server.ThreadingHTTPServer((host, port), tcp_handler)
            except OSError as e:
                print("端口 %d 启动失败：%s" % (port, e))
                if not unix_path:
                    sys.exit(1)
        else:
            for p in range(port, port + 11):
                try:
                    tcp = http.server.ThreadingHTTPServer((host, p), tcp_handler)
                    port = p
                    break
                except OSError:
                    continue
            if tcp is None:
                print("端口 %s~%s 均被占用，无法启动" % (port, port + 10))
                sys.exit(1)
    if tcp is not None:
        servers.append(tcp)
        url = "http://%s:%d" % (host if host != "0.0.0.0" else "127.0.0.1", port)
        print("=" * 52)
        print("  音纽 · 歌单导入")
        print("  已启动：%s  （Ctrl+C 退出）" % url)
        print("=" * 52)
        if not args["no_browser"]:
            threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    if not servers:
        print("没有可用的监听方式")
        sys.exit(1)
    for s in servers[1:]:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        servers[0].serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")
    finally:
        for s in servers:
            try:
                s.server_close()
            except Exception:
                pass
        if unix_path and os.path.exists(unix_path):
            try:
                os.unlink(unix_path)
            except OSError:
                pass


if __name__ == "__main__":
    main()
