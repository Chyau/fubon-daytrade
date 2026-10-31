# -*- coding: utf-8 -*-
"""多標的真實歷史資料最終判決：原版 vs 過濾版（90 天）"""
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

sdk, acc = fo.connect_sdk(fo.fubon_cfg())
try:
    sdk.init_realtime()
except Exception:
    pass

CFG_BASE = {**strategy.DEFAULTS}
CFG_FILTER = {**strategy.DEFAULTS, "min_vol_ratio": 1.5, "max_lot_risk": 20000}

all_raw, all_filt = [], []
per_sym = []
for sym in SYMBOLS:
    try:
        bars = fo.fetch_fubon_history(sym, days_back=90, log=None)
        if not bars:
            print(f"{sym}: 無資料，略過")
            continue
        days_n = len(set(b["date"] for b in bars))
        t_raw = bt.run_backtest(bars, CFG_BASE, sym, "MULTI-raw", log, save=False, use_rules=False)
        t_filt = bt.run_backtest(bars, CFG_FILTER, sym, "MULTI-filt", log, save=False, use_rules=False)
        all_raw += t_raw
        all_filt += t_filt
        per_sym.append((sym, days_n, len(t_raw), sum(t["net_pnl"] or 0 for t in t_raw),
                        len(t_filt), sum(t["net_pnl"] or 0 for t in t_filt)))
        print(f"{sym}: {days_n} 日｜原版 {len(t_raw)} 筆 {sum(t['net_pnl'] or 0 for t in t_raw):+,.0f}"
              f"｜過濾版 {len(t_filt)} 筆 {sum(t['net_pnl'] or 0 for t in t_filt):+,.0f}")
    except Exception as e:
        print(f"{sym}: 錯誤 {str(e)[:60]}")

try:
    sdk.logout()
except Exception:
    pass

p_raw = bt.performance(all_raw) if all_raw else None
p_filt = bt.performance(all_filt) if all_filt else None
print()
print("=" * 70)
print(f"多標的真實資料總判決（{len(per_sym)} 檔 × 90 天，交易樣本 {p_raw['n'] if p_raw else 0} 筆）")
print("=" * 70)
if p_raw and p_filt:
    print(f"{'':<10}{'筆數':>6}{'勝率':>8}{'總淨損益':>12}{'獲利因子':>9}{'期望值':>10}{'平均R':>8}")
    for name, p in (("原版", p_raw), ("過濾版", p_filt)):
        pf = "∞" if p["pf"] == float("inf") else f"{p['pf']:.2f}"
        print(f"{name:<10}{p['n']:>6}{p['win_rate']:>7.1f}%{p['total']:>12,.0f}{pf:>9}"
              f"{p['avg']:>10,.0f}{p['avg_r']:>8.2f}")
    print("-" * 70)
    print(f"標籤分布（原版）：{p_raw['tags']}")
    print(f"標籤分布（過濾）：{p_filt['tags']}")
