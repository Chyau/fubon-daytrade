# -*- coding: utf-8 -*-
"""intraday_loop.py — 本機盤中自動重放迴圈（由 Windows 工作排程於週一～五 09:04 呼叫）

每 15 分鐘執行一次 paper_step --push（本地重放＋推送雲端），
直到 14:50 自動退出。週末啟動自保。不依賴 GitHub 排程（本機 100% 可靠）。
"""
import io
import subprocess
import sys
import time
from datetime import datetime, time as dtime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
LOG_DIR = BASE / "logs"
END = dtime(14, 50)      # 迴圈結束時刻（含收盤安全網）
INTERVAL = 15 * 60       # 15 分鐘

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
LOG_DIR.mkdir(exist_ok=True)


def log(msg):
    line = f"[{datetime.now():%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    with open(LOG_DIR / "scheduler.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def main():
    if datetime.now().weekday() >= 5:
        log("週末 — 不執行")
        return 0
    log(f"盤中迴圈啟動（每 {INTERVAL // 60} 分鐘一次，至 {END}）")
    py = sys.executable
    n = 0
    while True:
        now = datetime.now()
        if now.time() >= END:
            log(f"已達 {END}，迴圈結束（本日共 {n} 次重放）")
            break
        try:
            r = subprocess.run([py, str(BASE / "tools" / "paper_step.py"), "--push"],
                               cwd=BASE, capture_output=True, text=True, timeout=600)
            n += 1
            tail = (r.stdout or "").strip().splitlines()
            log(f"重放 #{n} exit={r.returncode}｜{(tail[-1] if tail else '')[:80]}")
        except subprocess.TimeoutExpired:
            n += 1
            log(f"重放 #{n} 逾時（600s）— 跳過本輪")
        except Exception as e:
            log(f"重放異常：{str(e)[:100]}")
        # 對齊下一個 15 分鐘整數時刻
        now2 = time.time()
        nxt = (int(now2) // INTERVAL + 1) * INTERVAL
        time.sleep(max(5, nxt - now2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
