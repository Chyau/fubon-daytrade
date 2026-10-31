# -*- coding: utf-8 -*-
"""
佛系當沖法回測器 — 依逐字稿四階段規則，輸出交易績效＋完整交易日誌
==================================================================
資料來源：
  --demo          合成 5 分K（趨勢日／盤整日／假跌破日），離線驗證管線
  --csv PATH      5 分K CSV（欄位：datetime 或 date+time，open/high/low/close[, volume]）
  --yf SYMBOL     yfinance 5 分K（例：2426.TW；5m 最長 60 天）

輸出：
  - 交易績效報告：勝率／獲利因子／盈虧比／期望值／平均 R／最大回撤／結果標籤分布
  - sqlite3（data/trades.db）：trades＋trade_events＋stop_updates＋signals_log＋equity_curve
  - logs/fubon_daytrade.log：DEBUG 全量日誌

用法：
  python backtest.py --demo
  python backtest.py --demo --risk-budget 5000
  python backtest.py --csv data/2426_5m.csv --symbol 2426
  python backtest.py --yf 2426.TW --period 5d
  python backtest.py --history 10
"""
import argparse
import csv
import os
import sys
import time
import uuid

from logutil import get_logger, ensure_utf8_stdout

ensure_utf8_stdout()

import db as tdb       # noqa: E402
import strategy        # noqa: E402


# ==================== 資料載入 ====================
def load_csv(path, log=None):
    bars = []
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            cols = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items() if k}
            dt = cols.get("datetime") or cols.get("time") or ""
            if cols.get("date"):
                date = cols["date"]
                hhmm = cols.get("hhmm") or cols.get("time") or (dt[-5:] if len(dt) >= 5 else "")
                time_s = f"{date} {hhmm}"
            else:
                date, hhmm, time_s = dt[:10], dt[11:16], dt
            try:
                bars.append({"date": date, "hhmm": hhmm, "time": time_s,
                             "open": float(cols["open"]), "high": float(cols["high"]),
                             "low": float(cols["low"]), "close": float(cols["close"]),
                             "volume": float(cols.get("volume") or 0)})
            except (KeyError, ValueError):
                if log:
                    log.warning(f"CSV 列格式跳過：{row}")
    if log:
        log.info(f"CSV 載入 {len(bars)} 根 5 分K（{path}）")
    return bars


def fetch_yf(symbol, period="5d", log=None):
    """yfinance 5 分K（台股 5m 上限約 60 天）。symbol 可給 2426 或 2426.TW"""
    try:
        import yfinance as yf
    except ImportError:
        if log:
            log.error("未安裝 yfinance：pip install yfinance")
        return []
    cands = [symbol] if "." in symbol else [f"{symbol}.TW", f"{symbol}.TWO"]
    df = used = None
    for s in cands:
        try:
            d = yf.download(s, period=period, interval="5m", progress=False, auto_adjust=False)
        except Exception as e:
            if log:
                log.warning(f"yfinance {s} 下載失敗：{e}")
            d = None
        if d is not None and not d.empty:
            df, used = d, s
            break
    if df is None or df.empty:
        if log:
            log.error(f"yfinance 無資料：{symbol}")
        return []
    if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
        df.columns = df.columns.get_level_values(0)
    bars = []
    for idx, r in df.iterrows():
        try:
            o, h, l, c = float(r["Open"]), float(r["High"]), float(r["Low"]), float(r["Close"])
        except (TypeError, ValueError):
            continue
        if any(x != x for x in (o, h, l, c)):  # NaN
            continue
        idx2 = (idx.tz_localize("Asia/Taipei") if idx.tzinfo is None
                else idx.tz_convert("Asia/Taipei"))
        ts = idx2.to_pydatetime()
        bars.append({"date": ts.strftime("%Y-%m-%d"), "hhmm": ts.strftime("%H:%M"),
                     "time": ts.strftime("%Y-%m-%d %H:%M"),
                     "open": round(o, 2), "high": round(h, 2),
                     "low": round(l, 2), "close": round(c, 2),
                     "volume": float(r.get("Volume", 0) or 0)})
    if log:
        log.info(f"yfinance 取得 {used}：{len(bars)} 根 5 分K")
    return bars


def make_demo_bars(days=15, seed=42, log=None):
    """合成 5 分K（週期：趨勢下跌／盤整／假跌破），離線驗證管線與學習標籤"""
    import random
    from datetime import date as _date, timedelta
    rnd = random.Random(seed)
    times = [f"{(540 + m) // 60:02d}:{(540 + m) % 60:02d}" for m in range(0, 270, 5)]
    bars, made, d = [], 0, 0
    today = _date.today()
    while made < days:
        day = today - timedelta(days=d)
        d += 1
        if day.weekday() >= 5:
            continue
        ptype = made % 3  # 0 趨勢下跌（進場＋停損下移）／1 盤整（不進場）／2 假跌破（跳空停損）
        px = 90.0 + rnd.uniform(-4, 4)
        c = None
        for i, tm in enumerate(times):
            if i == 0:
                o = round(px, 2)
                c = (round(o + rnd.uniform(-0.12, 0.12), 2) if ptype == 1
                     else round(o * (1 - rnd.uniform(0.012, 0.02)), 2))  # 開盤長黑K
            elif ptype == 2 and i == 1:
                o = round(c * 1.02, 2)  # 假跌破：隔根跳空高開 → 觸發停損
                c = round(o * 1.001, 2)
            elif ptype == 2:
                o, c = c, round(c * (1 + rnd.uniform(-0.001, 0.001)), 2)
            elif ptype == 0:
                o = c
                step = rnd.uniform(0.005, 0.008) if i % 4 == 0 else rnd.uniform(0.0, 0.0012)
                c = round(o * (1 - step), 2)
            else:
                o = c
                c = round(o + rnd.uniform(-0.15, 0.15), 2)
            big = abs(o - c) >= 0.005 * o
            hi = round(max(o, c) + rnd.uniform(0.02, 0.12) * (1 if big else 2.5), 2)
            lo = round(min(o, c) - rnd.uniform(0.02, 0.1) * (1 if big else 2.0), 2)
            bars.append({"date": day.isoformat(), "hhmm": tm,
                         "time": f"{day.isoformat()} {tm}",
                         "open": o, "high": hi, "low": lo, "close": c,
                         "volume": rnd.randint(500, 5000)})
        made += 1
    if log:
        log.debug(f"合成 {made} 個交易日 × {len(times)} 根 5 分K")
    return bars


# ==================== 回測主流程 ====================
def run_backtest(bars, cfg, symbol, run_id, log, save=True, use_rules=True):
    """save=False：純模擬不入庫（參數掃描/A-B 用）；use_rules=False：不套用核准規則（原版對照）"""
    days = {}
    for b in bars:
        days.setdefault(b["date"], []).append(b)
    rules = []
    conn = None
    if save or use_rules:
        conn = tdb.connect()
        tdb.init_db(conn)
        if use_rules:
            rules = [dict(r) for r in tdb.list_rules(conn, approved_only=True)]
            if rules:
                log.info(f"載入 {len(rules)} 條核准的避坑規則（引擎將自動擋下符合進場）")
    trades = []
    for date in sorted(days):
        res = strategy.run_day(days[date], cfg, symbol, date, log=log, rules=rules)
        for sig in res["signals"]:
            if save:
                tdb.record_signal(conn, sig["date"], sig["symbol"], sig["ts"],
                                  sig["event"], sig["reason"], sig["detail"])
            log.debug(f"訊號：{sig['event']} {sig['reason']} @ {sig['ts']}")
        t = res["trade"]
        if t:
            t["run_id"] = run_id
            if save:
                dup = tdb.trade_exists(conn, t)
                tid = tdb.record_trade(conn, t)   # record_trade 自動標記 is_dup
                if dup:
                    log.debug(f"重複交易（is_dup 標記，學習統計排除）：{symbol} {date} {t['entry_time']}")
                else:
                    log.debug(f"交易 #{tid} 入庫（事件 {len(t['events'])}、"
                              f"停損更新 {len(t['stop_updates'])}）")
            trades.append(t)
    if save:
        cum = 0.0
        for t in sorted(trades, key=lambda x: (x["date"], x["exit_time"] or "")):
            cum += t["net_pnl"]
            tdb.record_equity(conn, run_id, t["date"], round(cum, 2))
        conn.close()
        log.info(f"回測完成 run_id={run_id}：{len(days)} 個交易日、{len(trades)} 筆交易"
                 f"（已寫入 {tdb.DB_PATH}）")
        try:
            import notify
            if trades:
                rep = performance(trades)
                notify.send(f"📊 回測完成 {symbol}｜{rep['n']} 筆 淨 {rep['total']:+,.0f} 元｜"
                            f"勝率 {rep['win_rate']:.0f}%｜期望值 {rep['avg']:+,.0f} 元/筆")
        except Exception:
            pass
    elif conn is not None:
        conn.close()
    return trades


def sweep_params(bars, symbol, base_cfg, log):
    """長黑K定義網格校準（body_mult × min_body_pct）——純模擬不入庫"""
    days = {}
    for b in bars:
        days.setdefault(b["date"], []).append(b)
    results = []
    for bm in (1.0, 1.25, 1.5, 1.75, 2.0):
        for mbp in (0.3, 0.4, 0.5, 0.7, 1.0):
            cfg = {**base_cfg, "body_mult": bm, "min_body_pct": mbp}
            trades = []
            for date in sorted(days):
                res = strategy.run_day(days[date], cfg, symbol, date)
                if res["trade"]:
                    trades.append(res["trade"])
            results.append((bm, mbp, performance(trades)))
    results.sort(key=lambda r: r[2]["total"], reverse=True)
    print("=" * 66)
    print("長黑K定義網格掃描（body_mult × min_body_pct，依總淨損益排序）")
    print("=" * 66)
    print(f"{'body_mult':>9}{'min_body%':>10}{'筆數':>5}{'勝率':>7}"
          f"{'總淨損益':>12}{'獲利因子':>9}{'平均R':>8}")
    for bm, mbp, rep in results:
        pf = "∞" if rep["pf"] == float("inf") else f"{rep['pf']:.2f}"
        print(f"{bm:>9}{mbp:>10}{rep['n']:>5}{rep['win_rate']:>6.0f}%"
              f"{rep['total']:>12,.0f}{pf:>9}{rep['avg_r']:>8.2f}")
    print("-" * 66)
    print("⚠ 樣本內最佳化有過擬合風險：請以不同期間／標的交叉驗證後再採用。")


# ==================== 交易績效 ====================
def performance(trades):
    n = len(trades)
    nets = [t["net_pnl"] for t in trades]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    cum = peak = 0.0
    mdd = 0.0
    for x in nets:
        cum += x
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    streak = worst = 0
    for x in nets:
        streak = streak + 1 if x <= 0 else 0
        worst = max(worst, streak)
    tags = {}
    for t in trades:
        tags[t["result_tag"]] = tags.get(t["result_tag"], 0) + 1
    return {
        "n": n, "wins": len(wins), "losses": len(losses),
        "win_rate": len(wins) / n * 100 if n else 0.0,
        "total": sum(nets),
        "fees": sum(t["fees"] for t in trades),
        "tax": sum(t["tax"] for t in trades),
        "avg": sum(nets) / n if n else 0.0,
        "avg_win": sum(wins) / len(wins) if wins else 0.0,
        "avg_loss": sum(losses) / len(losses) if losses else 0.0,
        "payoff": ((sum(wins) / len(wins)) / abs(sum(losses) / len(losses)))
                  if wins and losses else float("inf"),
        "pf": sum(wins) / abs(sum(losses)) if losses else float("inf"),
        "avg_r": sum(t["r_multiple"] for t in trades) / n if n else 0.0,
        "mdd": mdd, "max_consec_loss": worst,
        "avg_bars": sum(t["bars_held"] for t in trades) / n if n else 0.0,
        "tags": tags,
        "first_date": min(t["date"] for t in trades) if trades else "",
        "last_date": max(t["date"] for t in trades) if trades else "",
    }


def print_report(rep, trades, run_id, symbol):
    B = "=" * 64
    pf = "∞" if rep["pf"] == float("inf") else f"{rep['pf']:.2f}"
    po = "∞" if rep["payoff"] == float("inf") else f"{rep['payoff']:.2f}"
    print(B)
    print("交易績效報告（佛系當沖法回測）")
    print(f"run_id={run_id} ｜ 標的={symbol} ｜ 期間 {rep['first_date']} ~ {rep['last_date']}")
    print(B)
    print(f"交易數 {rep['n']} ｜ 勝 {rep['wins']} / 敗 {rep['losses']} ｜ 勝率 {rep['win_rate']:.1f}%")
    print(f"總淨損益 {rep['total']:+,.0f} 元（手續費 -{rep['fees']:,.0f}｜交易稅 -{rep['tax']:,.0f}）")
    print(f"平均每筆 {rep['avg']:+,.0f}｜平均獲利 {rep['avg_win']:+,.0f}｜平均虧損 {rep['avg_loss']:+,.0f}")
    print(f"盈虧比 {po}｜獲利因子 {pf}｜期望值 {rep['avg']:+,.0f} 元/筆")
    wr = rep["win_rate"]
    e = wr / 100 * rep["avg_win"] + (1 - wr / 100) * rep["avg_loss"]
    print(f"  E ＝ {wr:.1f}%×{rep['avg_win']:+,.0f} − {100 - wr:.1f}%×{abs(rep['avg_loss']):,.0f} "
          f"＝ {e:+,.0f} 元/筆（R 口徑平均 {rep['avg_r']:.2f}R）")
    print(f"平均R {rep['avg_r']:.2f}｜最大連虧 {rep['max_consec_loss']} 筆｜最大回撤 {rep['mdd']:,.0f} 元")
    print(f"平均持倉 {rep['avg_bars']:.1f} 根（約 {rep['avg_bars'] * 5:.0f} 分鐘）")
    print(f"結果標籤分布：{rep['tags']}")
    print(B)
    print("每筆明細（新→舊）：")
    print(f"{'日期':<11}{'進':>6}{'出':>6}{'進價':>8}{'出價':>8} "
          f"{'出場原因':<13}{'結果標籤':<16}{'淨損益':>10}{'R':>7}")
    for t in sorted(trades, key=lambda x: (x["date"], x["exit_time"] or ""), reverse=True):
        print(f"{t['date']:<11}{t['entry_time']:>6}{t['exit_time'] or '':>6}"
              f"{t['entry_price']:>8}{t['exit_price'] or 0:>8} "
              f"{(t['exit_reason'] or ''):<13}{(t['result_tag'] or ''):<16}"
              f"{t['net_pnl']:>10,.0f}{t['r_multiple']:>7.2f}")


# ==================== CLI ====================
def main():
    ap = argparse.ArgumentParser(description="佛系當沖法回測器（四階段規則）")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--demo", action="store_true", help="合成資料離線驗證")
    src.add_argument("--csv", metavar="PATH", help="5 分K CSV")
    src.add_argument("--yf", metavar="SYMBOL", help="yfinance，例 2426.TW")
    src.add_argument("--fubon", metavar="SYMBOL", help="富邦 SDK 真實 5 分K（.env 測試憑證）")
    ap.add_argument("--days-back", type=int, default=10, help="--fubon 回溯天數")
    ap.add_argument("--symbol", help="標的代碼（寫入 DB；--demo 預設 DEMO，--csv 預設檔名）")
    ap.add_argument("--period", default="5d", help="yfinance 期數（5m 最長 60 天）")
    ap.add_argument("--days", type=int, default=15, help="--demo 合成天數")
    ap.add_argument("--body-mult", type=float, default=strategy.DEFAULTS["body_mult"])
    ap.add_argument("--body-window", type=int, default=strategy.DEFAULTS["body_window"])
    ap.add_argument("--min-body-pct", type=float, default=strategy.DEFAULTS["min_body_pct"])
    ap.add_argument("--body-range-ratio", type=float, default=strategy.DEFAULTS["body_range_ratio"])
    ap.add_argument("--entry-cutoff", default=strategy.DEFAULTS["entry_cutoff"])
    ap.add_argument("--force-exit", default=strategy.DEFAULTS["force_exit"])
    ap.add_argument("--risk-budget", type=float, default=0.0, help="單筆最大虧損預算（元）→ 自動算張數")
    ap.add_argument("--max-lot-risk", type=float, default=0.0, help="單張風險上限（元）：超過跳過進場")
    ap.add_argument("--lots", type=int, default=1)
    ap.add_argument("--fee-discount", type=float, default=strategy.DEFAULTS["fee_discount"])
    ap.add_argument("--min-fee", type=float, default=strategy.DEFAULTS["min_fee"])
    ap.add_argument("--tax-rate", type=float, default=strategy.DEFAULTS["tax_rate"])
    ap.add_argument("--history", type=int, metavar="N", help="查最近 N 筆回測交易後離開")
    ap.add_argument("--sweep", action="store_true", help="長黑K定義網格掃描（純模擬不入庫）")
    ap.add_argument("--no-rules", action="store_true", help="不套用核准規則（原版對照）")
    ap.add_argument("--ab-rules", action="store_true", help="A/B 比較：無規則 vs 套用核准規則")
    ap.add_argument("--min-vol-ratio", type=float, default=0.0,
                    help="觸發K最小量能比（1.5＝只做放量長黑，過濾縮量假跌破）")
    ap.add_argument("--entry-mode", choices=["next_open", "breakout"], default="next_open",
                    help="進場模式：breakout＝等突破觸發K高低點（逐字稿爆量技巧）")
    ap.add_argument("--market-filter", action="store_true",
                    help="大盤風向過濾（加權指數日漲跌：漲=跳過放空、跌=跳過做多）")
    ap.add_argument("--log-level", default=None, choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    a = ap.parse_args()
    log = get_logger("backtest", a.log_level)

    if a.history:
        conn = tdb.connect()
        tdb.init_db(conn)
        rows = tdb.list_trades(conn, a.history)
        print(f"最近 {len(rows)} 筆回測交易：")
        for r in rows:
            print(f"  #{r['id']} {r['date']} {r['symbol']} 進{r['entry_price']}@{r['entry_time']} "
                  f"出{r['exit_price']}@{r['exit_time']} {r['exit_reason']}/{r['result_tag']} "
                  f"淨{r['net_pnl']:+,.0f} R={r['r_multiple']}")
        conn.close()
        return

    if a.demo:
        bars = make_demo_bars(a.days, log=log)
        symbol = a.symbol or "DEMO"
    elif a.csv:
        bars = load_csv(a.csv, log=log)
        symbol = a.symbol or os.path.splitext(os.path.basename(a.csv))[0]
    elif a.yf:
        bars = fetch_yf(a.yf, a.period, log)
        symbol = a.symbol or a.yf.split(".")[0]
    elif a.fubon:
        import fubon_orders as fo
        bars = fo.fetch_fubon_history(a.fubon, days_back=a.days_back, log=log)
        symbol = a.symbol or a.fubon
    else:
        ap.error("請指定資料來源：--demo / --csv / --yf / --fubon")
        return
    if not bars:
        log.error("無資料，回測中止")
        return

    if a.sweep:
        base_cfg = {"body_mult": a.body_mult, "body_window": a.body_window,
                    "min_body_pct": a.min_body_pct, "body_range_ratio": a.body_range_ratio,
                    "entry_cutoff": a.entry_cutoff, "force_exit": a.force_exit,
                    "risk_budget": a.risk_budget, "lots": a.lots,
                    "fee_discount": a.fee_discount, "min_fee": a.min_fee,
                    "tax_rate": a.tax_rate}
        sweep_params(bars, symbol, base_cfg, log)
        return

    market_map = None
    if a.market_filter:
        try:
            import yfinance as yf
            df = yf.download("^TWII", period="6mo", interval="1d",
                             progress=False, auto_adjust=False)
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
            market_map = daily
            log.info(f"大盤風向過濾啟用：{len(daily)} 個交易日")
        except Exception as e:
            log.warning(f"大盤資料取得失敗，過濾停用：{e}")

    cfg = {"body_mult": a.body_mult, "body_window": a.body_window,
           "min_body_pct": a.min_body_pct, "body_range_ratio": a.body_range_ratio,
           "entry_cutoff": a.entry_cutoff, "force_exit": a.force_exit,
           "risk_budget": a.risk_budget, "lots": a.lots,
           "max_lot_risk": a.max_lot_risk, "min_vol_ratio": a.min_vol_ratio,
           "entry_mode": a.entry_mode, "market_bias_map": market_map,
           "fee_discount": a.fee_discount, "min_fee": a.min_fee, "tax_rate": a.tax_rate}

    if a.ab_rules:  # A/B：無規則 vs 套用核准規則（純模擬不入庫）
        run_id = time.strftime("%Y%m%d-%H%M%S") + "-AB"
        t0 = run_backtest(bars, cfg, symbol, run_id + "-無規則", log, save=False, use_rules=False)
        t1 = run_backtest(bars, cfg, symbol, run_id + "-有規則", log, save=False, use_rules=True)
        p0, p1 = performance(t0), performance(t1)
        print("=" * 66)
        print(f"A/B 規則績效比較（{symbol}，樣本資料 {len(bars)} 根）")
        print("=" * 66)
        print(f"{'':<10}{'筆數':>5}{'勝率':>8}{'總淨損益':>12}{'期望值':>10}{'平均R':>8}")
        for name, p in (("無規則", p0), ("有規則", p1)):
            print(f"{name:<10}{p['n']:>5}{p['win_rate']:>7.1f}%{p['total']:>12,.0f}"
                  f"{p['avg']:>10,.0f}{p['avg_r']:>8.2f}")
        print("-" * 66)
        print(f"差異：淨損益 {p1['total'] - p0['total']:+,.0f} 元｜"
              f"交易數 {p1['n'] - p0['n']:+d} 筆（被規則擋下）｜"
              f"期望值 {p1['avg'] - p0['avg']:+,.0f} 元/筆")
        print("判讀：差異為正＝規則提升期望值（核准保留）；為負＝規則錯殺（撤銷或調整門檻）。")
        return

    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    trades = run_backtest(bars, cfg, symbol, run_id, log, use_rules=not a.no_rules)
    rep = performance(trades)
    print_report(rep, trades, run_id, symbol)


if __name__ == "__main__":
    main()
