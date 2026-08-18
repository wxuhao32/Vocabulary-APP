@echo off
title Vocabulary APP - Setup
cd /d "%~dp0"
echo ==================================================
echo   Vocabulary APP - 环境初始化
echo   创建虚拟环境并安装后端依赖
echo ==================================================
echo.
if exist ".venv\Scripts\python.exe" (
  echo [提示] 虚拟环境已存在，跳过创建。
) else (
  echo [1/3] 创建虚拟环境...
  python -m venv .venv
  if errorlevel 1 goto :err
)
echo [2/3] 安装依赖...
".venv\Scripts\pip.exe" install -r requirements.txt
if errorlevel 1 goto :err
echo [3/3] 完成!
echo.
echo 现在可以双击桌面的「启动CET4Prep服务器.bat」启动服务器。
pause
exit /b 0

:err
echo.
echo [错误] 初始化失败，请确认已安装 Python 3.10+ 并勾选 Add to PATH。
pause
