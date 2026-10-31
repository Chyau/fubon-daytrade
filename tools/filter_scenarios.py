# -*- coding: utf-8 -*-
"""過濾器情境分析：量能/實體過濾對真實交易樣本的影響"""
import io
import json
import sqlite3
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
conn = sqlite3.connect(r"C:\Users\Chyau\Documents\預設專案\fubon-daytrade\data\trades.db")
conn.row_factory = sqlite3.Row
rows = [dict(r) for r in conn.execute(
    "SELECT * FROM trades WHERE run_id LIKE 'ANATOMY%' ORDER BY id")]


def feat(t):
    try:
        return json.loads(t["features"]) if t["features"] else {}
    except Exception:
        return {}


def stats(ts, label):
    if not ts:
        print(f"{label:<34} 0 筆")
        return
    n = len(ts)
    wins = sum(1 for t in ts if (t["net_pnl"] or 0) > 0)
    net = sum(t["net_pnl"] or 0 for t in ts)
    avg_r = sum(t["r_multiple"] or 0 for t in ts) / n
    print(f"{label:<34} {n:>3} 筆｜勝率 {wins / n * 100:5.1f}%｜淨損益 {net:>+10,.0f}｜平均R {avg_r:+.2f}")


print(f"=== 真實資料過濾器情境分析（樣本 {len(rows)} 筆）===")
stats(rows, "(1) 無過濾（現況）")

f1 = [t for t in rows if (feat(t).get("vol_ratio") or 0) >= 1.0]
stats(f1, "(2) + 量能比 >= 1.0（排除縮量）")

f2 = [t for t in f1 if (feat(t).get("vol_ratio") or 0) >= 1.5]
stats(f2, "(3) + 量能比 >= 1.5（放量確認）")

f3 = [t for t in rows if (feat(t).get("body_pct") or 0) >= 1.0]
stats(f3, "(4) + 實體 >= 1.0%（排除小實體）")

f4 = [t for t in rows
      if (feat(t).get("vol_ratio") or 0) >= 1.5 and (feat(t).get("body_pct") or 0) >= 1.0]
stats(f4, "(5) + 量能>=1.5 且 實體>=1.0%（雙重）")

if f4:
    rev = sum(1 for t in f4 if t["result_tag"] == "R_STOP_REVERSAL")
    print(f"   （情境 5 中假跌破 {rev}/{len(f4)} 筆）")

print("\n=== 各過濾器的取捨明細（擋掉什麼）===")
for name, cond in (("量能比 >= 1.0", lambda f: (f.get("vol_ratio") or 0) >= 1.0),
                   ("量能比 >= 1.5", lambda f: (f.get("vol_ratio") or 0) >= 1.5),
                   ("實體 >= 1.0%", lambda f: (f.get("body_pct") or 0) >= 1.0)):
    blocked = [t for t in rows if not cond(feat(t))]
    if blocked:
        b_net = sum(t["net_pnl"] or 0 for t in blocked)
        b_rev = sum(1 for t in blocked if t["result_tag"] == "R_STOP_REVERSAL")
        b_win = sum(1 for t in blocked if (t["net_pnl"] or 0) > 0)
        print(f"  {name:<12} 擋掉 {len(blocked):>3} 筆（假跌破 {b_rev}、"
              f"獲利單 {b_win}、擋掉的淨損益 {b_net:+,.0f}）")
