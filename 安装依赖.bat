@echo off
chcp 65001 >nul
title 错题本 - 安装依赖
cd /d "%~dp0"

echo 正在安装 Python 依赖...
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo 依赖安装失败，请检查 Python、网络和 pip 配置。
  pause
  exit /b 1
)

echo.
python tools\check_environment.py
echo.
echo Python 依赖安装完成。PDF 组卷还需要单独安装 TeX Live 或 MiKTeX。
pause
