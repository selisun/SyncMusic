# fpk 打包与安装说明（在 NAS 上操作）

本目录（`fpk/`）是一个完整的 fnOS 原生应用工程。**打包必须在 fnOS（Linux）上进行**，Windows 上无法出包。全程约 5 分钟。

## 一、前置条件

- fnOS NAS 一台，能 SSH 登录（管理员账号）
- 官方打包工具 `fnpack`：SSH 登录后执行 `which fnpack` 检查。若没有，到飞牛官方开发者文档/社区下载对应架构的 fnpack 放进 PATH（或与工程同目录）

## 二、打包步骤

1. 把整个 `fpk` 文件夹传到 NAS 上（飞牛文件管理器拖进去，或 `scp -r fpk 用户名@NAS_IP:/tmp/nd2f`）

2. SSH 登录 NAS，进入工程目录：

   ```bash
   cd /tmp/nd2f        # 换成你实际放的路径
   ```

3. 修复 Windows 换行符并加执行权限（保险操作，必须执行）：

   ```bash
   sed -i 's/\r$//' manifest config/privilege config/resource app/ui/config cmd/*
   chmod +x cmd/*
   ```

4. 打包：

   ```bash
   fnpack build        # 若提示无此子命令，试 fnpack pack .
   ```

   成功后当前目录会生成 `SyncTune_1.0.0.fpk`（文件名可能略有差异）。

## 三、安装

1. 飞牛网页桌面 → **应用中心 → 右上角菜单 → 手动安装**，上传刚生成的 `.fpk`
2. 安装完成后，桌面出现「音纽」图标，点开即是工具界面（fnOS 窗口内打开）
3. 照界面提示填 Navidrome 和飞牛音乐的地址账号（飞牛地址填 `http://127.0.0.1:5666` 或 `http://NAS的IP:5666` 均可）

## 四、文件结构

```
fpk/
├── manifest                  应用信息（版本/端口/桌面入口，改版本号在这里）
├── config/
│   ├── privilege             以包用户（package）身份运行
│   └── resource              fnOS 窗口 iframe 模式，端口 8879
├── cmd/
│   ├── main                  服务启停脚本（start/stop/status）
│   └── install_* / upgrade_* / uninstall_* / config_*   生命周期脚本（空实现）
├── app/
│   ├── SyncMusic/            应用本体（app.py + web/index.html + web/reward.png 赞赏码）
│   └── ui/                   桌面入口 config + 多尺寸图标
├── ICON.PNG / ICON_256.PNG   应用中心图标
└── tools/                    icon_source.png（图标源图）+ make_icons.ps1（Windows 下改图标后重新生成各尺寸）
```

## 五、维护

- **升级版本**：改 `manifest` 里的 `version` 和 `changelog`，重新打包，手动安装上传即可覆盖升级
- **换赞赏码**：替换 `web/reward.png`（建议 480×480 的 PNG，文件名保持不变），无需改代码
- **注意**：打包前确认 `app/SyncMusic/web/` 下同时有 `index.html` 与 `reward.png`，否则赞赏弹层里的二维码会 404
- **看运行日志**：`/var/packages/SyncTune/var/server.log`
- **从旧包 navidrome2fnos 迁移**：卸载旧包后，其配置在 `/var/packages/navidrome2fnos/var/config.json`，可拷到 `/var/packages/SyncTune/var/` 复用（或重新填写）
- **卸载**：应用中心卸载；用户配置保留在数据目录，重装后无需重填
- **ARM 机器**：`manifest` 已是 `platform = all`。本项目不含任何架构相关二进制（纯 Python 标准库 + 静态页面），官方文档规定这种包应声明 `all` 以同时支持 x86 与 ARM；只有像 `arm` 那样限定单架构才需要改
- **端口被占用**：本项目只走网关 socket，不占用任何端口，正常不会遇到；若将来改回端口服务，需同时在 `manifest` 的 `service_port`、`config/resource` 与 `app/ui/config` 的 `port` 三处一起改（如 8879 → 8880）
- **桌面图标没刷新**：SSH 执行 `systemctl restart trim_app_center.service`

## 六、关于「上架飞牛应用中心」

- **官方应用中心**：需要与飞牛（成都飞牛私有云科技）企业合作，走官方上架流程，个人暂无自助上架入口
- **个人分发的现实路径**（本工程已按此标准制作）：
  1. 把 `.fpk` 发给别人，对方在应用中心「手动安装」上传即可
  2. 发布到 GitHub Releases，附 SHA256 校验
  3. 提交到社区第三方应用源（如 fn-knock 生态的 `moo.json` 源格式，含下载地址/SHA256/更新说明）
