@echo off
chcp 65001 >nul
title Karing 免费节点一键刷新
cd /d %~dp0
echo ============================================
echo   Karing 免费节点一键刷新
echo   拉取 -^> 筛选 -^> 测速 -^> 生成可更新的订阅
echo   全程约 3-15 分钟, 请勿关闭本窗口
echo ============================================
set PYTHONIOENCODING=utf-8
"E:\Development\Env\Python3.10.11\python.exe" karing_free_refresh.py %*
echo.
echo 订阅文件已写入 output\karing.yaml, 也可用「启动订阅服务.bat」提供固定订阅链接。
pause
