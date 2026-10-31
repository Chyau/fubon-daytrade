# -*- coding: utf-8 -*-
"""無前瞻偏誤版：用「前一日大盤方向」（開盤時可知）過濾"""
import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"C:\Users\Chyau\Documents\預設專案\fubon-daytrade")

from logutil import get_logger  # noqa: E402

log = get_logger("multitest")

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

# 無前瞻：date D 的偏誤 = 前一個交易日的方向（開盤時可知）
dates_sorted = sorted(daily.keys())
PREV_MARKET = {}
for i in range(1, len(dates_sorted)):
    PREV_MARKET[dates_sorted[i]] = daily[dates_sorted[i - 1]]
print(f"前一日大盤方向 map：{len(PREV_MARKET)} 個交易日（開盤時可知，無前瞻偏誤）")

sdk, acc = fo.connect_sdk(fo.fubon_cfg())
try:
    sdk.init_realtime()
except Exception:
    pass

CFG_RAW = {**strategy.DEFAULTS}
CFG_MKT_PREV = {**strategy.DEFAULTS, "market_bias_map": PREV_MARKET}

all_raw, all_prev = [], []
for sym in SYMBOLS:
    try:
        bars = fo.fetch_fubon_history(sym, days_back=90, log=None)
        if not bars:
            continue
        t_raw = bt.run_backtest(bars, CFG_RAW, sym, "P-raw", log, save=False, use_rules=False)
        t_prev = bt.run_backtest(bars, CFG_MKT_PREV, sym, "P-prev", log, save=False, use_rules=False)
        all_raw += t_raw
        all_prev += t_prev
        s = lambda t: sum(x["net_pnl"] or 0 for x in t)
        print(f"{sym}: 原版 {len(t_raw)} 筆 {s(t_raw):+,.0f}｜前日風向過濾 {len(t_prev)} 筆 {s(t_prev):+,.0f}")
    except Exception as e:
        print(f"{sym}: 錯誤 {str(e)[:60]}")

try:
    sdk.logout()
except Exception:
    pass

print()
print("=" * 70)
print(f"無前瞻偏誤判決（90 天 × {len(SYMBOLS)} 檔，樣本 {len(all_raw)} 筆）")
print("=" * 70)
for name, ts in (("1) 原版（無過濾）", all_raw),
                 ("2) 前一日大盤方向過濾", all_prev)):
    if not ts:
        print(f"{name}: 0 筆")
        continue
    p = bt.performance(ts)
    pf = "INF" if p["pf"] == float("inf") else f"{p['pf']:.2f}"
    print(f"{name:<20}{p['n']:>5} 筆｜勝率 {p['win_rate']:5.1f}%｜淨損益 {p['total']:>+12,.0f}"
          f"｜PF {pf}｜期望值 {p['avg']:>+8,.0f}｜平均R {p['avg_r']:+.2f}")
print("-" * 70)
print(f"標籤分布（前日風向過濾）：{bt.performance(all_prev)['tags'] if all_prev else '—'}")
