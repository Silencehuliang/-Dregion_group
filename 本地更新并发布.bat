@echo off
chcp 65001 >nul
title Karing 本地验证更新 + 发布到 Cloudflare
cd /d %~dp0
echo ============================================
echo   本地验证更新 + 发布到 Cloudflare
echo   在「你自己这台机器」的网络下验证节点
echo   验证通过 -^> 推送到线上订阅地址
echo   约 3-6 分钟, 请勿关闭本窗口
echo ============================================
echo.
set PYTHONIOENCODING=utf-8
"E:\Development\Env\Python3.10.11\python.exe" region_group.py --no-color
if errorlevel 1 (
  echo.
  echo [X] 生成失败, 或本地验证后没有可用节点。
  echo     已保留线上旧订阅, 不发布。
  echo.
  pause
  exit /b 1
)
echo.
echo --- 推送到 Cloudflare KV ---
"E:\Development\Env\Python3.10.11\python.exe" publish_cloudflare.py
if errorlevel 1 (
  echo.
  echo [X] 推送失败。多为网络抖动(脚本已自动重试 4 次)。
  echo     重跑一次通常就好; 若持续失败再检查本目录 .env 里的 CF_* 配置。
  echo.
  pause
  exit /b 1
)
echo.
echo [OK] 线上订阅已更新(内容为本机验证过的节点)。
echo      地址: https://sub.silencehl.eu.org/sub
echo      到 Karing 里刷新该订阅即可。
echo.
pause
