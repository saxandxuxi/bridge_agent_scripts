#!/usr/bin/env bash
# 启动 Web 管理台（Linux）
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export REPORT_WEB_TOKEN="${REPORT_WEB_TOKEN:-change-me}"
export REPORT_WEB_HOST="${REPORT_WEB_HOST:-0.0.0.0}"
export REPORT_WEB_PORT="${REPORT_WEB_PORT:-8080}"
export REPORT_WEB_MODE="${REPORT_WEB_MODE:-bridge}"
export REPORT_PROJECT_ROOT="$ROOT"
echo "启动 Web 管理台: http://$REPORT_WEB_HOST:$REPORT_WEB_PORT"
cd "$ROOT"
python web/app.py
