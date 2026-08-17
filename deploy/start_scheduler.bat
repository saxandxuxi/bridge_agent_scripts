@echo off
rem Start quarterly scheduler (Windows, no conda activation needed).
rem Usage: start_scheduler.bat <bridge_id>
rem Called by scheduled task BridgeReportScheduler at boot.
setlocal

set "BRIDGE_ID=%~1"
if "%BRIDGE_ID%"=="" set "BRIDGE_ID=xiangjiang"

set "PROJECT_ROOT=%~dp0.."
set "PYTHONW=C:\ProgramData\miniconda3\envs\bridge\pythonw.exe"
if not exist "%PYTHONW%" set "PYTHONW=C:\ProgramData\miniconda3\pythonw.exe"
if not exist "%PYTHONW%" set "PYTHONW=pythonw.exe"

cd /d "%PROJECT_ROOT%"
if not exist "%PROJECT_ROOT%\outputs\logs" mkdir "%PROJECT_ROOT%\outputs\logs"
"%PYTHONW%" serve_scheduler.py --bridge "%BRIDGE_ID%" >> "%PROJECT_ROOT%\outputs\logs\scheduler_console.log" 2>&1
