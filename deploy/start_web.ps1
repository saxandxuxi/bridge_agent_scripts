# 启动 Web 管理台（Windows）
# 用法：powershell -ExecutionPolicy Bypass -File deploy\start_web.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot

# 访问令牌（生产环境必改；留空则不鉴权，仅建议本机调试）
$env:REPORT_WEB_TOKEN = if ($env:REPORT_WEB_TOKEN) { $env:REPORT_WEB_TOKEN } else { "change-me" }
$env:REPORT_WEB_HOST = "0.0.0.0"          # 监听所有网卡（配合防火墙/nginx）
$env:REPORT_WEB_PORT = "8080"
$env:REPORT_WEB_MODE = "bridge"           # bridge=单机 / hub=中心汇总
$env:REPORT_PROJECT_ROOT = $Root

Write-Host "启动 Web 管理台: http://$env:REPORT_WEB_HOST`:$env:REPORT_WEB_PORT"
Write-Host "令牌: $env:REPORT_WEB_TOKEN"
Set-Location $Root
python web/app.py
