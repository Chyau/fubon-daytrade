#!/usr/bin/env bash
# 快照推送（Linux 版，對應 push_snapshot.bat）：匯出 → commit → push → Vercel 自動部署
set -e
cd "$(dirname "$0")"
PY="python3"; [ -x ".venv/bin/python" ] && PY=".venv/bin/python"
"$PY" tools/export_snapshot.py "$@"
"$PY" tools/lock_snapshot.py snapshot/index.html docs/index.html
"$PY" longshort.py >/dev/null 2>&1
git add -f data/longshort_symbols.json
git add snapshot
git commit -q -m "Update read-only snapshot" --only snapshot || echo "(No snapshot changes to commit)"
git pull --rebase -q || true
git push
