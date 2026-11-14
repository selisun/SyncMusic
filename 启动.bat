@echo off
title Navidrome - 飞牛音乐 歌单导入
cd /d "%~dp0"

if exist "%~dp0runtime\python.exe" (
  echo 正在启动，浏览器将自动打开 http://127.0.0.1:8787
  echo 关闭本窗口即退出工具。
  echo.
  "%~dp0runtime\python.exe" app.py
  echo.
  echo 程序已退出。若上方有报错信息，请截图反馈。
  pause
  exit /b 0
)

echo [错误] 未找到 runtime\python.exe，工具文件不完整，请重新解压。
pause
exit /b 1
