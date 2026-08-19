@echo off
rem 启动 Web 管理台（Windows，双击或命令行运行）。
rem 默认端口 8456；被占用可改成 8457 等并同步放行防火墙。
setlocal

set "PROJECT_ROOT=%~dp0"
if "%PROJECT_ROOT:~-1%"=="\" set "PROJECT_ROOT=%PROJECT_ROOT:~0,-1%"

set "PYTHONW=C:\ProgramData\miniconda3\envs\bridge\pythonw.exe"
if not exist "%PYTHONW%" set "PYTHONW=C:\ProgramData\miniconda3\pythonw.exe"
if not exist "%PYTHONW%" set "PYTHONW=pythonw.exe"

rem 访问令牌（生产环境务必改成随机值；留空则不鉴权，仅建议本机调试）
if "%REPORT_WEB_TOKEN%"=="" set "REPORT_WEB_TOKEN=change-me"
if "%REPORT_WEB_HOST%"=="" set "REPORT_WEB_HOST=0.0.0.0"
if "%REPORT_WEB_PORT%"=="" set "REPORT_WEB_PORT=8456"
set "REPORT_PROJECT_ROOT=%PROJECT_ROOT%"

cd /d "%PROJECT_ROOT%"
if not exist "%PROJECT_ROOT%\outputs\logs" mkdir "%PROJECT_ROOT%\outputs\logs"
"%PYTHONW%" web\app.py >> "%PROJECT_ROOT%\outputs\logs\web_console.log" 2>&1
