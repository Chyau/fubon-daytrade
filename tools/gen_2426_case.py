# -*- coding: utf-8 -*-
"""
建立 2426 鼎元逐字稿案例 5 分K CSV（2026-08-18）
================================================
依《亞當理論》進階版逐字稿數值重建（data/ 遺失後可重新產生）：
  09:00 開盤首根長黑K O=73.2 / C=72.1（空方表態）
  09:05 進場 72.1；該根亦為長黑K → 停損 73.2→72.1
  09:30–09:40 連續三根長黑K（第5根開盤 69.8）→ 停損一路下移
  13:20 時間出場 @ 67.3 → 單張淨利 4,632 元（手續費三折、稅減半）

用法：python tools/gen_2426_case.py
驗證：python backtest.py --csv data/2426_case_0818.csv --symbol 2426
"""
import csv
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROWS = []


def add(hhmm, o, h, l, c, v=1000):
    ROWS.append(("2026-08-18", hhmm, o, h, l, c, v))


# --- 觸發段 ---
add("09:00", 73.2, 73.3, 72.0, 72.1, 3120)   # 長黑K①
add("09:05", 72.1, 72.2, 71.4, 71.5, 2860)   # 進場 72.1；長黑K② → 停損 73.2→72.1
# --- 緩跌段（小實體，不出場、不再觸發） ---
add("09:10", 71.5, 71.6, 71.2, 71.3, 1400)
add("09:15", 71.3, 71.4, 71.0, 71.1, 1180)
add("09:20", 71.1, 71.2, 70.8, 70.9, 1060)
add("09:25", 70.9, 71.0, 70.6, 70.7, 980)
# --- 加速段：連續三根長黑K（09:30–09:40，第5根開盤=69.8） ---
add("09:30", 71.2, 71.3, 70.3, 70.4, 2600)   # 長黑K③ → 停損→71.2
add("09:35", 70.4, 70.5, 69.5, 69.6, 3050)   # 長黑K④ → 停損→70.4
add("09:40", 69.8, 69.9, 68.8, 68.9, 3400)   # 長黑K⑤ → 停損→69.8
# --- 趨勢末段（小實體緩跌，高點不觸及 69.8） ---
price = 68.9
times = [f"{(540 + m) // 60:02d}:{(540 + m) % 60:02d}" for m in range(45, 260, 5)]  # 09:45–13:15
for i, tm in enumerate(times):
    o = price
    c = round(o - 0.04, 2)
    ROWS.append(("2026-08-18", tm, o, round(o + 0.05, 2), round(c - 0.05, 2), c, 800 - i * 10))
    price = c
# --- 時間出場 ---
add("13:20", 67.4, 67.5, 67.2, 67.3, 2100)   # X_TIME_1320 → 買回 67.3
add("13:25", 67.3, 67.4, 67.2, 67.3, 900)
add("13:30", 67.3, 67.4, 67.2, 67.35, 1500)

out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "data", "2426_case_0818.csv")
os.makedirs(os.path.dirname(out), exist_ok=True)


def main():
    with open(out, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "time", "open", "high", "low", "close", "volume"])
        for r in ROWS:
            w.writerow(r)
    print(f"✓ 已產生 {out}（{len(ROWS)} 根）")


if __name__ == "__main__":
    main()
