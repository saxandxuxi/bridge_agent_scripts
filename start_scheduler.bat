@echo off
rem 启动常驻调度器（Windows，双击或命令行运行）。
rem 用法：start_scheduler.bat <bridge_id>，例如 start_scheduler.bat mishuihe
setlocal

set "BRIDGE_ID=%~1"
if "%BRIDGE_ID%"=="" (
  echo 用法: start_scheduler.bat ^<bridge_id^>
  echo 例如: start_scheduler.bat mishuihe
  echo 可用 bridge_id 见 bridges\registry.json
  pause
  exit /b 1
)

set "PROJECT_ROOT=%~dp0"
if "%PROJECT_ROOT:~-1%"=="\" set "PROJECT_ROOT=%PROJECT_ROOT:~0,-1%"

set "PYTHONW=C:\ProgramData\miniconda3\envs\bridge\pythonw.exe"
if not exist "%PYTHONW%" set "PYTHONW=C:\ProgramData\miniconda3\pythonw.exe"
if not exist "%PYTHONW%" set "PYTHONW=pythonw.exe"

cd /d "%PROJECT_ROOT%"
if not exist "%PROJECT_ROOT%\outputs\logs" mkdir "%PROJECT_ROOT%\outputs\logs"
"%PYTHONW%" serve_scheduler.py --bridge "%BRIDGE_ID%" >> "%PROJECT_ROOT%\outputs\logs\scheduler_console.log" 2>&1
