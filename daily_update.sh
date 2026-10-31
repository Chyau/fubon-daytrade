#!/usr/bin/env bash
# 盤後資料更新（Linux 版，對應 daily_update.bat）
cd "$(dirname "$0")"
PY="python3"; [ -x ".venv/bin/python" ] && PY=".venv/bin/python"
exec "$PY" tools/daily_update.py "$@"
