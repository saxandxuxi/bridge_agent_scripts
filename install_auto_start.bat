@echo off
rem 注册开机自启（请以管理员身份运行）。
rem 用法：install_auto_start.bat <bridge_id>，例如 install_auto_start.bat mishuihe
setlocal

set "BRIDGE_ID=%~1"
if "%BRIDGE_ID%"=="" (
  echo 用法: install_auto_start.bat ^<bridge_id^>
  echo 例如: install_auto_start.bat mishuihe
  pause
  exit /b 1
)

set "ROOT=%~dp0"

schtasks /Create /F /TN "BridgeReportWeb" /TR "\"%ROOT%start_web.bat\"" /SC ONSTART /RU SYSTEM /RL HIGHEST
if errorlevel 1 (
  echo 注册 Web 自启失败（请以管理员身份运行本脚本）
  pause
  exit /b 1
)

schtasks /Create /F /TN "BridgeReportScheduler" /TR "\"%ROOT%start_scheduler.bat\" %BRIDGE_ID%" /SC ONSTART /RU SYSTEM /RL HIGHEST
if errorlevel 1 (
  echo 注册调度器自启失败（请以管理员身份运行本脚本）
  pause
  exit /b 1
)

echo.
echo 已注册开机自启：
echo   BridgeReportWeb        -> start_web.bat
echo   BridgeReportScheduler  -> start_scheduler.bat %BRIDGE_ID%
echo 服务器重启后会自动启动 Web 和调度器。
pause
