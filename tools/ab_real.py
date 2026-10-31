# -*- coding: utf-8 -*-
"""13 檔 x 60 天真實資料：原版 vs 風控過濾版"""
import io
import json
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"C:\Users\Chyau\Documents\預設專案\fubon-daytrade")

from logutil import get_logger  # noqa: E402

log = get_logger("ab")

SYMBOLS = ["2301", "2382", "2454", "2883", "3231", "2303", "3008", "1605",
           "2327", "2345", "2308", "2357", "2360"]

import fubon_orders as fo    # noqa: E402
import backtest as bt        # noqa: E402
import strategy              # noqa: E402

sdk, acc = fo.connect_sdk(fo.fubon_cfg())
try:
    sdk.init_realtime()
except Exception:
    pass

CFG_RAW = {**strategy.DEFAULTS}
CFG_RISK = {**strategy.DEFAULTS, "risk_budget": 5000}

all_raw, all_risk = [], []
for sym in SYMBOLS:
    try:
        bars = fo.fetch_fubon_history(sym, days_back=60, log=None)
        if not bars:
            continue
        t1 = bt.run_backtest(bars, CFG_RAW, sym, "AB-raw", log, save=False, use_rules=False)
        t2 = bt.run_backtest(bars, CFG_RISK, sym, "AB-risk", log, save=False, use_rules=False)
        all_raw += t1
        all_risk += t2
        s = lambda t: sum(x["net_pnl"] or 0 for x in t)
        print(f"{sym}: raw {len(t1)} {s(t1):+,.0f} | risk {len(t2)} {s(t2):+,.0f}")
    except Exception as e:
        print(f"{sym}: ERR {str(e)[:60]}")

try:
    sdk.logout()
except Exception:
    pass

out = {"raw": bt.performance(all_raw), "risk": bt.performance(all_risk)}
print("RESULTS=" + json.dumps(
    {k: {kk: vv for kk, vv in v.items() if kk != "tags"} for k, v in out.items()},
    ensure_ascii=False))
