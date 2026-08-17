@echo off
rem Start web console (Windows, no conda activation needed).
rem Called by scheduled task BridgeReportWeb at boot.
setlocal

set "PROJECT_ROOT=%~dp0.."
set "PYTHONW=C:\ProgramData\miniconda3\envs\bridge\pythonw.exe"
if not exist "%PYTHONW%" set "PYTHONW=C:\ProgramData\miniconda3\pythonw.exe"
if not exist "%PYTHONW%" set "PYTHONW=pythonw.exe"

rem Access token (change to a random value in production; keep if already set)
if "%REPORT_WEB_TOKEN%"=="" set "REPORT_WEB_TOKEN=change-me"
if "%REPORT_WEB_HOST%"=="" set "REPORT_WEB_HOST=0.0.0.0"
if "%REPORT_WEB_PORT%"=="" set "REPORT_WEB_PORT=8080"
if "%REPORT_WEB_MODE%"=="" set "REPORT_WEB_MODE=bridge"
set "REPORT_PROJECT_ROOT=%PROJECT_ROOT%"

cd /d "%PROJECT_ROOT%"
if not exist "%PROJECT_ROOT%\outputs\logs" mkdir "%PROJECT_ROOT%\outputs\logs"
"%PYTHONW%" web\app.py >> "%PROJECT_ROOT%\outputs\logs\web_console.log" 2>&1
