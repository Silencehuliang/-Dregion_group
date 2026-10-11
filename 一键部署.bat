@echo off
chcp 65001 >nul
title 一键部署到 Cloudflare
cd /d %~dp0

set "GITBASH="
for %%P in (
  "%ProgramFiles%\Git\bin\bash.exe"
  "%ProgramFiles(x86)%\Git\bin\bash.exe"
  "%LOCALAPPDATA%\Programs\Git\bin\bash.exe"
  "E:\Development\Tool\Git\bin\bash.exe"
) do if not defined GITBASH if exist %%P set "GITBASH=%%~P"
if not defined GITBASH set "GITBASH=bash"

"%GITBASH%" "%~dp0deploy_setup.sh"

echo.
pause
