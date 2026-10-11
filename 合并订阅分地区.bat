@echo off
chcp 65001 >nul
title Karing 合并上游订阅 + 按地区分组
cd /d %~dp0
echo ============================================
echo   合并上游订阅 + 按地区分组
echo   拉取上游订阅 -^> 去重合并 -^> 生成带地区分组的订阅
echo   约 1-3 分钟, 请勿关闭本窗口
echo ============================================
set PYTHONIOENCODING=utf-8
"E:\Development\Env\Python3.10.11\python.exe" region_group.py %*
echo.
echo 订阅文件已写入 output\karing_region.yaml
echo 在 Karing 里「添加订阅 - 从文件导入」选它即可。
pause
