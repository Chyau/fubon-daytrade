# -*- coding: utf-8 -*-
"""
analyze_candle_anatomy.py — 長黑K實體/影線/假突破 與 盈虧關聯分析
==================================================================
流程：
  1. 富邦 SDK 登入一次（.env 測試憑證）
  2. 批量抓「今日 5 分K」（標的：掃描器推薦＋盤前清單，預設 30 檔）
  3. 逐檔跑四階段規則 → 收集觸發交易（特徵含 K 棒解剖學快照）
  4. 入庫（run_id=ANATOMY-日期，供日後 pits 挖掘）
  5. 輸出影線分組 vs 盈虧 ＋ 假突破(R_STOP_REVERSAL)對照

用法：python tools/analyze_candle_anatomy.py [--n 30] [--min-branch 30]
誠實限制：樣本為「測試環境模擬行情＋單一交易日」，結論屬方向性參考。
"""
import argparse
import os
import sys
from collections import defaultdict

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from logutil import get_logger, ensure_utf8_stdout  # noqa: E402

ensure_utf8_stdout()
log = get_logger("anatomy")


def _feat(t):
    import json
    try:
        return json.loads(t["features"]) if t["features"] else {}
    except Exception:
        return {}


def bucket(v, edges):
    """依邊界切分桶（左閉右開，最後一桶含右端）"""
    for i in range(len(edges) - 1):
        if edges[i] <= v < edges[i + 1]:
            return f"{edges[i]:.2f}~{edges[i + 1]:.2f}"
    if v >= edges[-1]:
        return f">={edges[-1]:.2f}"
    return f"<{edges[0]:.2f}"


def group_stats(trades, keyfn):
    groups = defaultdict(list)
    for t in trades:
        try:
            groups[keyfn(t)].append(t)
        except Exception:
            continue
    out = []
    for k in sorted(groups):
        rs = groups[k]
        n = len(rs)
        wins = sum(1 for r in rs if (r["net_pnl"] or 0) > 0)
        revs = sum(1 for r in rs if r.get("result_tag") == "R_STOP_REVERSAL")
        avg_net = sum(r["net_pnl"] or 0 for r in rs) / n
        avg_r = sum(r["r_multiple"] or 0 for r in rs) / n
        out.append({"key": k, "n": n, "wins": wins, "win_pct": wins / n * 100,
                    "avg_net": avg_net, "avg_r": avg_r, "rev_pct": revs / n * 100})
    return out


def print_group(title, stats):
    print(f"\n--- {title} ---")
    print(f"{'分組':<14}{'筆數':>5}{'勝率':>8}{'假跌破率':>9}{'平均損益':>12}{'平均R':>8}")
    for g in stats:
        print(f"{g['key']:<14}{g['n']:>5}{g['win_pct']:>7.0f}%{g['rev_pct']:>8.0f}%"
              f"{g['avg_net']:>12,.0f}{g['avg_r']:>8.2f}")


def main():
    ap = argparse.ArgumentParser(description="長黑K解剖學 vs 盈虧 關聯分析")
    ap.add_argument("--n", type=int, default=30, help="分析標的數上限")
    ap.add_argument("--min-branch", type=int, default=30)
    ap.add_argument("--log-level", default=None, choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    a = ap.parse_args()
    log_ = log if a.log_level is None else __import__("logutil").get_logger("anatomy", a.log_level)

    import db as tdb            # noqa: E402
    import strategy             # noqa: E402
    import chips as chips_mod   # noqa: E402
    import fubon_orders as fo   # noqa: E402

    # 1) 標的清單：最新的盤前清單（不限當日）→ 籌碼候選 → 掃描器推薦
    import glob
    codes = []
    wls = sorted(glob.glob(os.path.join(BASE, "data", "watchlist_*.txt")))
    if wls:
        for ln in open(wls[-1], encoding="utf-8"):
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                codes.append(ln.split()[0])
    try:
        D = chips_mod.load_dataset()
        for _, info in chips_mod.daytrade_short_candidates(D, a.min_branch, top_n=a.n):
            if info["code"] not in codes:
                codes.append(info["code"])
    except Exception as e:
        log_.warning(f"籌碼候選載入失敗（僅用清單）：{e}")
    codes = codes[:a.n]
    if not codes:
        print("無標的可分析（請先執行 chips.py --watchlist）")
        return
    print(f"分析標的 {len(codes)} 檔：{','.join(codes)}")

    # 2) 登入一次，批量取行情＋跑規則
    sdk, acc = fo.connect_sdk(fo.fubon_cfg())
    try:
        sdk.init_realtime()
    except Exception:
        pass
    cfg = {**strategy.DEFAULTS}
    trades = []
    from datetime import date as _d, timedelta as _td
    run_id = f"ANATOMY-{_d.today():%Y%m%d}"
    conn = tdb.connect()
    tdb.init_db(conn)
    for sym in codes:
        try:
            bars = fo.fetch_intraday_bars(sdk, sym)
        except Exception as e:
            log_.warning(f"{sym} 行情失敗：{str(e)[:60]}")
            continue
        if not bars:
            continue
        date = bars[0]["date"]
        res = strategy.run_day(bars, cfg, sym, date)
        t = res["trade"]
        if t:
            t["run_id"] = run_id
            tdb.record_trade(conn, t)
            trades.append(t)
            log_.info(f"[{sym}] 進{t['entry_price']} 出{t['exit_price']}（{t['exit_reason']}）"
                      f"淨{t['net_pnl']:+,.0f} 標籤 {t['result_tag']}")
    try:
        sdk.logout()
    except Exception:
        pass
    conn.close()
    if not trades:
        print("今日無觸發交易（樣本 0）— 換標的或改日再跑")
        return

    # 3) 分組分析
    print("=" * 62)
    print(f"長黑K解剖學 vs 盈虧（樣本 {len(trades)} 筆，run_id={run_id}）")
    print("=" * 62)
    print_group("下影線/實體 比（越高＝跌下去被買回＝假跌破前兆）",
                group_stats(trades, lambda t: bucket(_feat(t).get("lower_sh_ratio", 0), [0, 0.2, 0.5, 1.01])))
    print_group("上影線/實體 比（越高＝反彈被壓回＝空方強）",
                group_stats(trades, lambda t: bucket(_feat(t).get("upper_sh_ratio", 0), [0, 0.2, 0.5, 1.01])))
    print_group("實體佔全K振幅（越高＝K棒越乾淨）",
                group_stats(trades, lambda t: bucket(_feat(t).get("body_range_ratio", 1), [0.6, 0.8, 0.95, 1.01])))
    print_group("收盤位置 close_pos（1＝收在最低＝空方完勝）",
                group_stats(trades, lambda t: bucket(_feat(t).get("close_pos", 0.5), [0, 0.5, 0.85, 1.01])))
    print_group("量能比（當根量÷前N根均量；爆量＝真實賣壓）",
                group_stats(trades, lambda t: (lambda v: "盤初無歷史量" if v is None
                                               else bucket(v, [0, 1, 2, 3, 99]))(_feat(t).get("vol_ratio"))))
    print_group("K 線型態（觸發K型態命名）",
                group_stats(trades, lambda t: _feat(t).get("pattern", "?")))

    # 4) 假突破（R_STOP_REVERSAL）對照
    rev = [t for t in trades if t.get("result_tag") == "R_STOP_REVERSAL"]
    good = [t for t in trades if t.get("result_tag") in ("R_TREND_RUN", "R_TRAIL_LOCK", "R_GAP_AWAY")]
    print("\n--- 假突破（R_STOP_REVERSAL）vs 順利單 對照 ---")
    if rev and good:
        def mean(rs, k):
            vals = [_feat(r).get(k) for r in rs if _feat(r).get(k) is not None]
            return sum(vals) / len(vals) if vals else float("nan")
        for k, label in (("lower_sh_ratio", "下影線/實體"), ("upper_sh_ratio", "上影線/實體"),
                         ("close_pos", "收盤位置"), ("body_range_ratio", "實體佔振幅"),
                         ("body_pct", "實體%"), ("vol_ratio", "量能比")):
            print(f"  {label:<10} 假跌破平均 {mean(rev, k):.2f} ｜ 順利單平均 {mean(good, k):.2f}")
        print(f"  樣本：假跌破 {len(rev)} 筆｜順利單 {len(good)} 筆")
    else:
        print(f"  （假跌破 {len(rev)} 筆／順利單 {len(good)} 筆 — 樣本不足，累積後再驗證）")
    print("\n⚠ 樣本為測試環境模擬行情＋單一交易日 — 結論屬方向性參考，請累積多日驗證。")


if __name__ == "__main__":
    main()
