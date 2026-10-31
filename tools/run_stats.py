# -*- coding: utf-8 -*-
"""run_id=20261031-110546 完整績效（13 檔真實資料）"""
import io
import json
import sqlite3
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
conn = sqlite3.connect(r"C:\Users\Chyau\Documents\預設專案\fubon-daytrade\data\trades.db")
conn.row_factory = sqlite3.Row
run_id = "20261031-110546-eb3547"
rows = [dict(r) for r in conn.execute(
    "SELECT * FROM trades WHERE run_id=?", (run_id,)).fetchall()]
n = len(rows)
nets = [t["net_pnl"] or 0 for t in rows]
wins = [x for x in nets if x > 0]
losses = [x for x in nets if x <= 0]
gross_w = sum(wins)
gross_l = abs(sum(losses))
pf = gross_w / gross_l if gross_l else float("inf")
ar = sum(t["r_multiple"] or 0 for t in rows) / n if n else 0
print(f"樣本 {n} 筆｜勝率 {len(wins)/n*100:.1f}%")
print(f"總淨損益 {sum(nets):+,.0f}｜平均 {sum(nets)/n:+,.0f}/筆")
print(f"平均獲利 {sum(wins)/len(wins):+,.0f}｜平均虧損 {sum(losses)/len(losses):+,.0f}")
pl = abs(sum(losses) / len(losses)) if losses else 1
pw = sum(wins) / len(wins) if wins else 0
print(f"盈虧比 {pw / pl:.2f}｜獲利因子 {pf:.2f}")
print(f"期望值 {sum(nets)/n:+,.0f} 元/筆｜平均R {ar:+.2f}")
streak = worst = 0
for x in nets:
    streak = streak + 1 if x <= 0 else 0
    worst = max(worst, streak)
print(f"最大單筆虧損 {min(nets):+,.0f}｜最大連虧 {worst} 筆")
tags = {}
for t in rows:
    tags[t["result_tag"]] = tags.get(t["result_tag"], 0) + 1
print("標籤分布:", tags)
