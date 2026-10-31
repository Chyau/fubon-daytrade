# -*- coding: utf-8 -*-
"""判決：進場模式（next_open vs breakout）× 大盤風向（無/前日）— 16 檔 × 90 天真實資料"""
import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"C:\Users\Chyau\Documents\預設專案\fubon-daytrade")

from logutil import get_logger  # noqa: E402

log = get_logger("judge")

SYMBOLS = ["2426", "2330", "2454", "3008", "2345", "2303", "1101", "3231",
           "5876", "4960", "3034", "3045", "3665", "2360", "1605", "3221"]

import fubon_orders as fo    # noqa: E402
import backtest as bt        # noqa: E402
import strategy              # noqa: E402
import yfinance as yf        # noqa: E402

df = yf.download("^TWII", period="6mo", interval="1d", progress=False, auto_adjust=False)
if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
    df.columns = df.columns.get_level_values(0)
daily, prev_c = {}, None
for idx, r in df.iterrows():
    c = r["Close"]
    if c != c:
        continue
    d = idx.strftime("%Y-%m-%d")
    if prev_c:
        daily[d] = "up" if float(c) > prev_c else "down"
    prev_c = float(c)
dates_sorted = sorted(daily.keys())
PREV_MARKET = {dates_sorted[i]: daily[dates_sorted[i - 1]] for i in range(1, len(dates_sorted))}

sdk, acc = fo.connect_sdk(fo.fubon_cfg())
try:
    sdk.init_realtime()
except Exception:
    pass

SCENARIOS = [
    ("1) next_open 原版", {"entry_mode": "next_open"}, False),
    ("2) breakout 突破", {"entry_mode": "breakout"}, False),
    ("3) next_open＋前日大盤", {"entry_mode": "next_open", "market_bias_map": PREV_MARKET}, False),
    ("4) breakout＋前日大盤", {"entry_mode": "breakout", "market_bias_map": PREV_MARKET}, False),
]

results = {name: [] for name, _, _ in SCENARIOS}
for sym in SYMBOLS:
    try:
        bars = fo.fetch_fubon_history(sym, days_back=90, log=None)
        if not bars:
            print(f"{sym}: 無資料，略過")
            continue
        for name, over, _u in SCENARIOS:
            cfg = {**strategy.DEFAULTS, **over}
            t = bt.run_backtest(bars, cfg, sym, f"J-{name}", log, save=False, use_rules=False)
            results[name] += t
        s = lambda t: sum(x["net_pnl"] or 0 for x in t)
        print(f"{sym}: 原版 {len(results[SCENARIOS[0][0]]) and ''}"
              f"{s(results[SCENARIOS[0][0]][-len(t_raw):]) if False else ''}"
              f"（本檔各情境已彙總）")
    except Exception as e:
        print(f"{sym}: 錯誤 {str(e)[:60]}")

try:
    sdk.logout()
except Exception:
    pass

print()
print("=" * 72)
print(f"進場模式 × 大盤風向 判決（16 檔 × 90 天真實資料）")
print("=" * 72)
for name, _over, _u in SCENARIOS:
    ts = results[name]
    if not ts:
        print(f"{name}: 0 筆")
        continue
    p = bt.performance(ts)
    pf = "INF" if p["pf"] == float("inf") else f"{p['pf']:.2f}"
    wins = sum(1 for t in ts if (t["net_pnl"] or 0) > 0)
    net = sum(t["net_pnl"] or 0 for t in ts)
    print(f"{name:<20}{p['n']:>5} 筆｜勝率 {wins / len(ts) * 100:5.1f}%｜淨損益 {net:>+12,.0f}"
          f"｜PF {pf}｜期望值 {net / len(ts):>+8,.0f}｜平均R {p['avg_r']:+.2f}")
print("-" * 72)
