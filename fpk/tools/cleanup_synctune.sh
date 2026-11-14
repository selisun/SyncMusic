#!/bin/bash
# 音纽(SyncTune) 卸载残留清理脚本 —— 需 root 执行
# 用法: sudo bash ~/cleanup_synctune.sh
# 清理范围: 应用安装目录、数据目录、系统用户/组；旧包配置先备份到 sean 家目录

echo '=== [1/5] 备份旧包配置 ==='
if [ -f '/vol1/@appdata/navidrome2fnos/config.json' ]; then
    cp '/vol1/@appdata/navidrome2fnos/config.json' /home/sean/synctune_config_backup.json \
        && chmod 644 /home/sean/synctune_config_backup.json \
        && echo '已备份: /home/sean/synctune_config_backup.json'
else
    echo '旧包配置不存在，跳过'
fi

echo '=== [2/5] 确认无残留进程 ==='
pkill -u SyncTune 2>/dev/null && echo '已终止 SyncTune 进程' || echo '无 SyncTune 进程'

echo '=== [3/5] 删除应用目录 ==='
rm -rf '/vol1/@appcenter/SyncTune' && echo '已删 /vol1/@appcenter/SyncTune'
rm -rf '/vol1/@appdata/SyncTune'   && echo '已删 /vol1/@appdata/SyncTune'
rm -rf '/vol1/@appdata/navidrome2fnos' && echo '已删 /vol1/@appdata/navidrome2fnos'

echo '=== [4/5] 删除系统用户和组 ==='
userdel SyncTune 2>/dev/null         && echo '已删用户 SyncTune'         || echo '用户 SyncTune 已不存在'
groupdel SyncTune 2>/dev/null        && echo '已删组 SyncTune'           || echo '组 SyncTune 已不存在'
userdel navidrome2fnos 2>/dev/null   && echo '已删用户 navidrome2fnos'   || echo '用户 navidrome2fnos 已不存在'
groupdel navidrome2fnos 2>/dev/null  && echo '已删组 navidrome2fnos'     || echo '组 navidrome2fnos 已不存在'

echo '=== [5/5] 清理后核验 ==='
echo '--- /vol1/@appcenter/ ---'
ls '/vol1/@appcenter/'
echo '--- /vol1/@appdata/ ---'
ls '/vol1/@appdata/'
echo '--- 用户/组检查（应无输出）---'
grep -iE 'synctune|navidrome2fnos' /etc/passwd /etc/group
echo '=== 清理完成，可回应用中心重新安装音纽(SyncTune) ==='
