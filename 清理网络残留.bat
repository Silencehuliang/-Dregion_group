@echo off
chcp 65001 >nul
cd /d %~dp0
echo 即将以管理员身份清理 Clash for Windows 残留(持久路由 + cfw-tap 虚拟网卡)。
echo 会弹出 UAC 窗口, 请点「是」。
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath powershell -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','%~dp0清理网络残留.ps1' -Verb RunAs"
echo.
echo 完成后可查看 logs\cleanup_*.log, 或直接看输出的「剩余持久路由」。
pause
