@echo off
chcp 65001 >nul
title Karing 订阅服务(常驻)
cd /d %~dp0
echo ============================================
echo   Karing 订阅服务
echo   固定订阅链接 + 定时自动刷新
echo   别关这个窗口; 关掉即停止服务
echo ============================================
set PYTHONIOENCODING=utf-8
"E:\Development\Env\Python3.10.11\python.exe" karing_free_refresh.py --serve --interval 1800
echo.
pause
