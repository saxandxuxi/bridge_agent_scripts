# Install Windows Scheduled Tasks:
#   - BridgeReportWeb       : start web console at boot (no conda activation needed)
#   - BridgeReportScheduler : quarterly report scheduler at boot
#
# Usage (Admin PowerShell):
#   powershell -ExecutionPolicy Bypass -File deploy\install_windows_tasks.ps1
# Optional params:
#   -BridgeId xiangjiang   (bridge id to auto-report)
#   -ProjectRoot D:\...    (project root, default = parent of this script)
#   -PythonEnv  C:\...\envs\bridge
#
# After install:
#   - Web auto-starts at boot: http://<server-ip>:8080
#   - Scheduler triggers on Jan/Apr/Jul/Oct 1st 08:00, generating the report
#     for the PREVIOUS completed quarter (e.g. Apr 1 -> Jan~Mar).
#   - Logs: outputs\logs\web_console.log / scheduler_console.log

param(
    [string]$BridgeId = "xiangjiang",
    [string]$ProjectRoot = "",
    [string]$PythonEnv = "C:\ProgramData\miniconda3\envs\bridge"
)

$ErrorActionPreference = "Stop"

if (-not $ProjectRoot) {
    $ProjectRoot = Split-Path -Parent $PSScriptRoot
}
$PythonW = Join-Path $PythonEnv "pythonw.exe"
if (-not (Test-Path $PythonW)) {
    $PythonW = Join-Path (Split-Path -Parent $PythonEnv) "pythonw.exe"
}
if (-not (Test-Path $PythonW)) {
    throw "pythonw.exe not found under conda env: $PythonEnv"
}

$WebBat = Join-Path $PSScriptRoot "start_web.bat"
$SchBat = Join-Path $PSScriptRoot "start_scheduler.bat"
if (-not (Test-Path $WebBat) -or -not (Test-Path $SchBat)) {
    throw "launcher scripts missing: $WebBat / $SchBat"
}

$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

# 1) Web console
$webArg = '/c "' + $WebBat + '"'
$webAction = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $webArg
$webTrigger = New-ScheduledTaskTrigger -AtStartup
Register-ScheduledTask -TaskName "BridgeReportWeb" `
    -Action $webAction -Trigger $webTrigger -Principal $principal -Settings $settings `
    -Description "Bridge report web console (auto start at boot)" -Force | Out-Null
Start-ScheduledTask -TaskName "BridgeReportWeb"
Write-Host "[OK] Scheduled task BridgeReportWeb installed & started"

# 2) Quarterly scheduler
$schArg = '/c "' + $SchBat + '" ' + $BridgeId
$schAction = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $schArg
$schTrigger = New-ScheduledTaskTrigger -AtStartup
Register-ScheduledTask -TaskName "BridgeReportScheduler" `
    -Action $schAction -Trigger $schTrigger -Principal $principal -Settings $settings `
    -Description "Quarterly report scheduler (bridge: $BridgeId)" -Force | Out-Null
Start-ScheduledTask -TaskName "BridgeReportScheduler"
Write-Host "[OK] Scheduled task BridgeReportScheduler installed & started (bridge=$BridgeId)"

Write-Host ""
Write-Host "Done."
Write-Host "  Web:       http://<server-ip>:8080  (token: see start_web.bat REPORT_WEB_TOKEN)"
Write-Host "  Quarterly: triggers on Jan/Apr/Jul/Oct 1st 08:00 for the previous quarter"
Write-Host "  Logs:      $ProjectRoot\outputs\logs\web_console.log / scheduler_console.log"
Write-Host "  Run once now:"
Write-Host "    & `"$PythonW`" run_agent.py --bridge $BridgeId --mode quarterly --date <last quarter end, e.g. 2026-03-31>"
