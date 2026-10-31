# -*- coding: utf-8 -*-
"""多標的真實歷史資料判決：原版 vs 量能過濾 vs 大盤風向過濾（90 天）"""
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
MARKET, prev_c = {}, None
for idx, r in df.iterrows():
    c = r["Close"]
    if c != c:
        continue
    d = idx.strftime("%Y-%m-%d")
    if prev_c:
        MARKET[d] = "up" if float(c) > prev_c else "down"
    prev_c = float(c)
print(f"大盤風向資料：{len(MARKET)} 個交易日")

sdk, acc = fo.connect_sdk(fo.fubon_cfg())
try:
    sdk.init_realtime()
except Exception:
    pass

CFG_RAW = {**strategy.DEFAULTS}
CFG_VOL = {**strategy.DEFAULTS, "min_vol_ratio": 1.5, "max_lot_risk": 20000}
CFG_MKT = {**strategy.DEFAULTS, "market_bias_map": MARKET}

all_raw, all_vol, all_mkt = [], [], []
for sym in SYMBOLS:
    try:
        bars = fo.fetch_fubon_history(sym, days_back=90, log=None)
        if not bars:
            print(f"{sym}: 無資料，略過")
            continue
        t_raw = bt.run_backtest(bars, CFG_RAW, sym, "M-raw", log, save=False, use_rules=False)
        t_vol = bt.run_backtest(bars, CFG_VOL, sym, "M-vol", log, save=False, use_rules=False)
        t_mkt = bt.run_backtest(bars, CFG_MKT, sym, "M-mkt", log, save=False, use_rules=False)
        all_raw += t_raw
        all_vol += t_vol
        all_mkt += t_mkt
        s = lambda t: sum(x["net_pnl"] or 0 for x in t)
        print(f"{sym}: 原版 {len(t_raw)} 筆 {s(t_raw):+,.0f}｜量能 {len(t_vol)} 筆 "
              f"{s(t_vol):+,.0f}｜市場 {len(t_mkt)} 筆 {s(t_mkt):+,.0f}")
    except Exception as e:
        print(f"{sym}: 錯誤 {str(e)[:60]}")

try:
    sdk.logout()
except Exception:
    pass

print()
print("=" * 70)
print(f"多標的真實資料三情境判決（90 天，交易樣本 {len(all_raw)} 筆）")
print("=" * 70)
for name, ts in (("1) 原版（無過濾）", all_raw),
                 ("2) + 量能過濾", all_vol),
                 ("3) + 大盤風向過濾", all_mkt)):
    if not ts:
        print(f"{name}: 0 筆")
        continue
    p = bt.performance(ts)
    pf = "INF" if p["pf"] == float("inf") else f"{p['pf']:.2f}"
    print(f"{name:<18}{p['n']:>5} 筆｜勝率 {p['win_rate']:5.1f}%｜淨損益 {p['total']:>+12,.0f}"
          f"｜PF {pf}｜期望值 {p['avg']:>+8,.0f}｜平均R {p['avg_r']:+.2f}")
print("-" * 70)
print(f"標籤分布（大盤風向過濾）：{bt.performance(all_mkt)['tags'] if all_mkt else '—'}")
