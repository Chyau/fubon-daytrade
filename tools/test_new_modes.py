# -*- coding: utf-8 -*-
"""test_new_modes.py — 三段新逐字稿模式的 A/B 實測（真實 yf 5 分K）

對照組：
  【空】做多…不對，放空宇宙（做空+偏空 24 檔）：
     原版 next_open（長黑K放空） vs 三要素 three_short
  【多】做多宇宙（做多+偏多 17 檔）：
     B1/B2 兩買點 b12_long vs 隔日沖買進 nextday_long

用法：python tools/test_new_modes.py [--period 5d]
"""
import argparse
import io
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import yfinance as yf  # noqa: E402
import longshort       # noqa: E402
import strategy        # noqa: E402
import backtest as bt  # noqa: E402


def fetch_bars(sym, period, log):
    for s in (f"{sym}.TW", f"{sym}.TWO"):
        try:
            d = yf.download(s, period=period, interval="5m",
                            progress=False, auto_adjust=False)
            if d is not None and not d.empty:
                if hasattr(d.columns, "nlevels") and d.columns.nlevels > 1:
                    d.columns = d.columns.get_level_values(0)
                bars = []
                for idx, r in d.iterrows():
                    try:
                        o, h, l, c = (float(r["Open"]), float(r["High"]),
                                      float(r["Low"]), float(r["Close"]))
                    except (TypeError, ValueError):
                        continue
                    if any(x != x for x in (o, h, l, c)):
                        continue
                    idx2 = (idx.tz_localize("Asia/Taipei") if idx.tzinfo is None
                            else idx.tz_convert("Asia/Taipei"))
                    ts = idx2.to_pydatetime()
                    bars.append({"date": ts.strftime("%Y-%m-%d"),
                                 "hhmm": ts.strftime("%H:%M"),
                                 "time": ts.strftime("%Y-%m-%d %H:%M"),
                                 "open": round(o, 2), "high": round(h, 2),
                                 "low": round(l, 2), "close": round(c, 2),
                                 "volume": float(r.get("Volume", 0) or 0)})
                return bars
        except Exception:
            continue
    return []


def summarize(name, trades):
    if not trades:
        print(f"  {name:<14} 0 筆")
        return
    nets = [t["net_pnl"] or 0 for t in trades]
    wins = [x for x in nets if x > 0]
    gp, gl = sum(wins), -sum(x for x in nets if x <= 0)
    pf = f"{gp / gl:.2f}" if gl > 0 else "∞"
    rs = [t["r_multiple"] for t in trades if t.get("r_multiple") is not None]
    print(f"  {name:<14} {len(trades):>3} 筆｜勝率 {len(wins) / len(trades) * 100:5.1f}%"
          f"｜淨 {sum(nets):+11,.0f}｜PF {pf}"
          f"｜avgR {(sum(rs) / len(rs)) if rs else 0:+.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", default="5d")
    a = ap.parse_args()
    from logutil import get_logger
    log = get_logger("test")

    _, short_rows = longshort.load_longshort()
    by = {}
    for r in short_rows:
        by.setdefault(r["bias_label"], []).append(r["stock_no"])
    short_u = by.get("做空", []) + by.get("偏空", [])
    long_u = by.get("做多", []) + by.get("偏多", [])
    print(f"宇宙：放空 {len(short_u)} 檔｜做多 {len(long_u)} 檔｜期數 {a.period}")

    cache = {}
    for i, sym in enumerate(sorted(set(short_u + long_u))):
        cache[sym] = fetch_bars(sym, a.period, log)
        if i % 8 == 0 and i:
            print(f"  …下載 {i}")
        time.sleep(0.6)
    got = sum(1 for v in cache.values() if v)
    print(f"取得資料：{got}/{len(cache)} 檔\n")

    print("=" * 74)
    print("【空】原版長黑K next_open vs 三要素 three_short（做空宇宙）")
    print("=" * 74)
    res = {"next_open": [], "three_short": []}
    for sym in short_u:
        bars = cache.get(sym)
        if not bars:
            continue
        for mode in res:
            cfg = {**strategy.DEFAULTS, "entry_mode": mode}
            ts = bt.run_backtest(bars, cfg, sym, f"TEST-{mode}", log, save=False)
            res[mode].extend(ts or [])
    summarize("next_open 原版", res["next_open"])
    summarize("three_short", res["three_short"])

    print()
    print("=" * 74)
    print("【多】B1/B2 兩買點 vs 隔日沖買進（做多宇宙）")
    print("=" * 74)
    res2 = {"b12_long": [], "nextday_long": []}
    # nextday_long 需要 20 日歷史 → 用日K（60d）；b12 用 5 分K
    daily_cache = {}
    for sym in long_u:
        bars = cache.get(sym)
        if not bars:
            continue
        cfg = {**strategy.DEFAULTS, "entry_mode": "b12_long"}
        ts = bt.run_backtest(bars, cfg, sym, "TEST-b12", log, save=False)
        res2["b12_long"].extend(ts or [])
        if sym not in daily_cache:
            try:
                dd = yf.download(f"{sym}.TW", period="60d", interval="1d",
                                 progress=False, auto_adjust=False)
                if dd is not None and not dd.empty:
                    if hasattr(dd.columns, "nlevels") and dd.columns.nlevels > 1:
                        dd.columns = dd.columns.get_level_values(0)
                    dbars = []
                    for idx, r in dd.iterrows():
                        try:
                            dbars.append({
                                "date": idx.strftime("%Y-%m-%d"),
                                "hhmm": "13:20",
                                "time": idx.strftime("%Y-%m-%d"),
                                "open": float(r["Open"]), "high": float(r["High"]),
                                "low": float(r["Low"]), "close": float(r["Close"]),
                                "volume": float(r.get("Volume", 0) or 0)})
                        except (TypeError, ValueError):
                            continue
                    daily_cache[sym] = dbars
                    time.sleep(0.5)
            except Exception:
                daily_cache[sym] = []
        cfg2 = {**strategy.DEFAULTS, "entry_mode": "nextday_long"}
        ts2 = bt.run_backtest(daily_cache.get(sym) or [], cfg2, sym,
                              "TEST-nextday", log, save=False)
        res2["nextday_long"].extend(ts2 or [])
    summarize("b12_long", res2["b12_long"])
    summarize("nextday_long", res2["nextday_long"])

    print("\n（皆為 yf 5 分K 實測；is_dup 概念不適用 — save=False 純模擬）")


if __name__ == "__main__":
    main()
