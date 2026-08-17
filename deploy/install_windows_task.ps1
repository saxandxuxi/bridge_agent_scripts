# 注册 Windows 计划任务：每周一 08:00 自动生成周报
# 用法：
#   .\install_windows_task.ps1                                # 使用默认值
#   .\install_windows_task.ps1 -Python "C:\Python311\python.exe" -WorkDir "D:\my_project"
#   .\install_windows_task.ps1 -WeeklyDay Monday -AtHour 8
#
# 环境变量覆盖（优先级低于参数）：
#   $env:REPORT_AGENT_PYTHON  — Python 解释器路径
#   $env:REPORT_AGENT_WORKDIR — 项目根目录

param(
    [string]$Python = $(if ($env:REPORT_AGENT_PYTHON) { $env:REPORT_AGENT_PYTHON } else { (Get-Command python -ErrorAction SilentlyContinue).Source }),
    [string]$WorkDir = $(if ($env:REPORT_AGENT_WORKDIR) { $env:REPORT_AGENT_WORKDIR } else { $PSScriptRoot | Split-Path -Parent }),
    [string]$WeeklyDay = "Monday",
    [int]$AtHour = 8,
    [int]$AtMinute = 0,
    [switch]$AlsoMonthly
)

$ErrorActionPreference = "Stop"

# 校验路径
if (-not $Python -or -not (Test-Path $Python)) {
    Write-Error "Python 路径无效: '$Python'。请通过 -Python 参数或 REPORT_AGENT_PYTHON 环境变量指定。"
    exit 1
}
if (-not (Test-Path $WorkDir)) {
    Write-Error "工作目录无效: '$WorkDir'。请通过 -WorkDir 参数或 REPORT_AGENT_WORKDIR 环境变量指定。"
    exit 1
}

$Script = Join-Path $WorkDir "run_agent.py"
if (-not (Test-Path $Script)) {
    Write-Error "未找到 run_agent.py: $Script"
    exit 1
}

$TriggerTime = "{0:D2}:{1:D2}am" -f $AtHour, $AtMinute

Write-Host "配置信息:"
Write-Host "  Python : $Python"
Write-Host "  WorkDir: $WorkDir"
Write-Host "  Script : $Script"
Write-Host "  时间   : 每周$WeeklyDay $TriggerTime"
Write-Host ""

# 周报
$actionWeekly = New-ScheduledTaskAction -Execute $Python `
    -Argument "`"$Script`" --mode weekly" -WorkingDirectory $WorkDir
$triggerWeekly = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $WeeklyDay -At $TriggerTime
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -DontStopOnIdleEnd -ExecutionTimeLimit (New-TimeSpan -Hours 2)
Register-ScheduledTask -TaskName "ReportAgent-Weekly" -Action $actionWeekly `
    -Trigger $triggerWeekly -Settings $settings `
    -Description "数据分析报告智能体 - 周报" -Force
Write-Host "已注册计划任务 ReportAgent-Weekly"

# 月报（可选）
if ($AlsoMonthly) {
    $actionMonthly = New-ScheduledTaskAction -Execute $Python `
        -Argument "`"$Script`" --mode monthly" -WorkingDirectory $WorkDir
    $triggerMonthly = New-ScheduledTaskTrigger -Monthly -DaysOfMonth 1 -At $TriggerTime
    Register-ScheduledTask -TaskName "ReportAgent-Monthly" -Action $actionMonthly `
        -Trigger $triggerMonthly -Settings $settings `
        -Description "数据分析报告智能体 - 月报" -Force
    Write-Host "已注册计划任务 ReportAgent-Monthly"
}

Write-Host ""
Get-ScheduledTask -TaskName "ReportAgent-*" | Format-Table TaskName, State
