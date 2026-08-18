@echo off
chcp 65001 >nul 2>&1
title Vocabulary APP - 下载 cloudflared
cd /d "%~dp0"
if exist cloudflared.exe ( echo cloudflared.exe 已存在，无需下载 & pause & exit /b 0 )
echo 正在下载 cloudflared.exe（Cloudflare Tunnel 客户端，约 60MB）...
echo 来源1: GitHub Release
curl -L --connect-timeout 20 --max-time 600 -o cloudflared.exe https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe
if not exist cloudflared.exe (
  echo 来源1失败，尝试来源2...
  curl -L --connect-timeout 20 --max-time 600 -o cloudflared.exe https://cloudflared.bowring.uk/binaries/cloudflared-windows-amd64-latest.exe
)
if exist cloudflared.exe (
  echo.
  echo 下载完成，版本信息：
  cloudflared.exe --version
) else (
  echo.
  echo [失败] 自动下载未成功。请手动下载后放到本目录：
  echo   https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe
  echo   下载后改名为 cloudflared.exe 放在 server	unnel\ 目录。
)
pause
