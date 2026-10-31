# -*- coding: utf-8 -*-
"""驗證大盤風向假說：放空交易的虧損是否集中在大盤上漲日"""
import io
import json
import sqlite3
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"C:\Users\Chyau\Documents\預設專案\fubon-daytrade")

import yfinance as yf

# 1) 加權指數日資料
df = yf.download("^TWII", period="6mo", interval="1d", progress=False, auto_adjust=False)
if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
    df.columns = df.columns.get_level_values(0)
daily = {}
prev_c = None
for idx, r in df.iterrows():
    c = r["Close"]
    if c != c:
        continue
    d = idx.strftime("%Y-%m-%d")
    if prev_c:
        daily[d] = (float(c) - prev_c) / prev_c * 100  # 當日漲跌%
    prev_c = float(c)
print(f"加權指數日資料：{len(daily)} 天")

# 2) 交易資料庫（排除合成/重放樣本）
conn = sqlite3.connect(r"C:\Users\Chyau\Documents\預設專案\fubon-daytrade\data\trades.db")
conn.row_factory = sqlite3.Row
rows = [dict(r) for r in conn.execute(
    "SELECT symbol, date, entry_time, exit_reason, result_tag, net_pnl, r_multiple "
    "FROM trades WHERE run_id NOT LIKE 'DEMO%' AND run_id NOT LIKE 'MULTI%' "
    "AND run_id NOT LIKE 'ANATOMY%' AND run_id NOT LIKE 'WEB%' AND run_id NOT LIKE 'LIVE%'")]
print(f"交易樣本（真實資料來源）: {len(rows)} 筆")

# 3) 依大盤漲跌分組
up, down, unknown = [], [], []
for t in rows:
    chg = daily.get(t["date"])
    if chg is None:
        unknown.append(t)
    elif chg > 0:
        up.append(t)
    else:
        down.append(t)


def stats(ts, label):
    if not ts:
        print(f"{label:<22} 0 筆")
        return
    n = len(ts)
    wins = sum(1 for t in ts if (t["net_pnl"] or 0) > 0)
    net = sum(t["net_pnl"] or 0 for t in ts)
    rev = sum(1 for t in ts if t["result_tag"] == "R_STOP_REVERSAL")
    ar = sum(t["r_multiple"] or 0 for t in ts) / n
    print(f"{label:<22} {n:>4} 筆｜勝率 {wins / n * 100:5.1f}%｜假跌破率 {rev / n * 100:5.1f}%"
          f"｜淨損益 {net:>+10,.0f}｜平均R {ar:+.2f}")


print()
stats(up, "大盤上漲日（逆風放空）")
stats(down, "大盤下跌日（順風放空）")
stats(unknown, "（無指數資料日）")

# 4) 大盤大跌日（跌 >1%）細分
big_down = [t for t in down if daily[t["date"]] <= -1]
mild = [t for t in down if daily[t["date"]] > -1]
stats(mild, "  其中大盤小跌（0~1%）")
stats(big_down, "  其中大盤大跌（>1%）")

# 5) 2426 案例日確認
for d in ("2026-08-18",):
    if d in daily:
        print(f"\n2426 案例日 {d} 加權指數漲跌：{daily[d]:+.2f}%")
