@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if exist "错题本.exe" (
  start "" "错题本.exe"
  exit /b 0
)

for /f "usebackq delims=" %%P in (`python -c "import sys; print(sys.executable)" 2^>nul`) do set "PYTHON_EXE=%%P"
if not defined PYTHON_EXE goto missing_python

"%PYTHON_EXE%" -c "import flask, customtkinter, PIL" >nul 2>nul
if errorlevel 1 goto missing_dependencies

set "PYTHONW_EXE=%PYTHON_EXE:python.exe=pythonw.exe%"
if exist "%PYTHONW_EXE%" (
  start "" "%PYTHONW_EXE%" launcher.py
  exit /b 0
)

start "" "%PYTHON_EXE%" launcher.py
exit /b 0

:missing_python
echo Python was not found. Build or install the desktop launcher first.
pause
exit /b 1

:missing_dependencies
echo Launcher dependencies are missing. Run install dependencies first.
pause
exit /b 1
