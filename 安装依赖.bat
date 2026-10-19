@echo off
chcp 65001 >nul
setlocal
title 错题本 - 安装依赖
cd /d "%~dp0"

set "CUOTI_PYTHON=python"
if exist "%CD%\.venv-stability\Scripts\python.exe" set "CUOTI_PYTHON=%CD%\.venv-stability\Scripts\python.exe"

echo 正在安装 Python 依赖...
"%CUOTI_PYTHON%" -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo 依赖安装失败，请检查 Python、网络和 pip 配置。
  pause
  exit /b 1
)

echo.
echo 正在配置项目 SQLite 补丁运行库...
"%CUOTI_PYTHON%" tools\sqlite_runtime.py install
if errorlevel 1 (
  echo SQLite 运行库配置失败，请使用项目 .venv-stability Python 并检查上面的提示。
  pause
  exit /b 1
)

echo.
"%CUOTI_PYTHON%" tools\check_environment.py
if errorlevel 1 (
  echo.
  echo Python 依赖已安装，但环境检查未通过，请按上面的提示处理。
  pause
  exit /b 1
)
echo.
echo Python 依赖安装完成。PDF 组卷还需要单独安装 TeX Live 或 MiKTeX。
pause
exit /b 0
