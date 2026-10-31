# -*- coding: utf-8 -*-
"""paper_step.py — 模擬下單單步（yfinance 資料 + 與富邦引擎完全相同的下單參數）

流程：
  1. 讀取多空分類清單（做空+偏空 24 檔）
  2. yfinance 批次抓最近 5 日 5 分K（單一請求，避免限流；.TW/.TWO 自動後備）
  3. 以回測引擎重放（四階段狀態機＋同一套下單參數），is_dup 自動去重
     → 盤中每 15 分鐘重放一次 = 持續累積的模擬交易紀錄
  4. 過濾 X_EOD（資料切斷點的假收尾）— 未平倉部位留待下次重放取得真實出場
  5. 快照雙輸出：snapshot/（Vercel）＋ docs/（GitHub Pages 備援）
     若設定 SNAPSHOT_PASSWORD → AES-256 加密（訪客需密碼解鎖）
  6. --push 時提交 data/trades.db＋快照 → Vercel/Pages 自動更新

用法：
  雲端 Actions：python tools/paper_step.py --push（密碼走 repo Secrets）
  在家本機　　：python tools/paper_step.py（只更新本機 DB＋快照）
"""
import argparse
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from logutil import get_logger, ensure_utf8_stdout  # noqa: E402

ensure_utf8_stdout()

import db as tdb        # noqa: E402
import strategy         # noqa: E402
import backtest as bt   # noqa: E402
import longshort        # noqa: E402


def df_to_bars(df, log=None):
    """yfinance DataFrame → 引擎 bars 格式（與 bt.fetch_yf 相同轉換）"""
    bars = []
    for idx, r in df.iterrows():
        try:
            o, h, l, c = (float(r["Open"]), float(r["High"]),
                          float(r["Low"]), float(r["Close"]))
        except (TypeError, ValueError):
            continue
        if any(x != x for x in (o, h, l, c)):  # NaN
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


def batch_fetch(syms, period, log):
    """逐檔循序下載（bt.fetch_yf 已處理 .TW/.TWO 後備）；平行轟炸會被 Yahoo 限流"""
    import time
    out = {}
    for i, sym in enumerate(syms):
        try:
            bars = bt.fetch_yf(sym, period=period, log=log)
            if bars:
                out[sym] = bars
        except Exception as e:
            log.warning(f"{sym} 下載失敗：{str(e)[:80]}")
        if i < len(syms) - 1:
            time.sleep(0.8)  # 循序節流，避免 Yahoo 限流
    return out


def main():
    ap = argparse.ArgumentParser(description="yfinance 模擬下單單步（重放＋去重＋快照）")
    ap.add_argument("--push", action="store_true", help="提交 trades.db＋快照到 GitHub")
    ap.add_argument("--period", default="5d", help="yfinance 抓取期數（預設 5d）")
    a = ap.parse_args()

    log = get_logger("paper")
    _, syms_str = longshort.short_symbols()
    syms = [s for s in syms_str.split(",") if s]
    if not syms:
        log.error("多空清單為空 — 檢查 longshort.py 的 DB 路徑")
        return 1

    # 下單參數與富邦引擎完全相同（DEFAULTS），固定 1 張（risk_budget=0）
    cfg = {**strategy.DEFAULTS, "risk_budget": 0}
    run_id = f"PAPER-{datetime.now():%Y%m%d}"

    log.info(f"批次下載 {len(syms)} 檔（{a.period} 5 分K）...")
    data = batch_fetch(syms, a.period, log)
    log.info(f"取得資料：{len(data)}/{len(syms)} 檔")

    conn = tdb.connect()
    tdb.init_db(conn)
    nodata = [s for s in syms if s not in data]
    new_trades = 0
    for sym in syms:
        bars = data.get(sym)
        if not bars:
            continue
        try:
            trades = bt.run_backtest(bars, cfg, sym, run_id, log,
                                     save=False, use_rules=True)
            for t in trades:
                if t.get("exit_reason") == "X_EOD":
                    continue  # 資料切斷點的假收尾 — 留待下次重放
                t["run_id"] = run_id
                dup = tdb.trade_exists(conn, t)
                tdb.record_trade(conn, t)
                if not dup:
                    new_trades += 1
                    log.info(f"新模擬交易：{sym} {t['date']} {t['entry_time']}"
                             f"→{t['exit_time']} 淨 {t['net_pnl']:+,.0f}"
                             f"（{t['exit_reason']}）")
        except Exception as e:
            log.warning(f"{sym} 重放失敗：{str(e)[:80]}")
    conn.close()

    # 快照雙輸出（Vercel 來源 + Pages 備援），再套密碼鎖（若有設定）
    for out in ("snapshot", "docs"):
        subprocess.run([sys.executable, str(BASE / "tools" / "export_snapshot.py"),
                        "--out", str(BASE / out)], check=True,
                       stdout=subprocess.DEVNULL)
    pw = os.environ.get("SNAPSHOT_PASSWORD", "").strip()
    if pw:
        subprocess.run([sys.executable, str(BASE / "tools" / "lock_snapshot.py"),
                        str(BASE / "snapshot" / "index.html"),
                        str(BASE / "docs" / "index.html")], check=True)
    else:
        log.info("SNAPSHOT_PASSWORD 未設定 — 快照明碼")

    log.info(f"paper step 完成：{len(data)}/{len(syms)} 檔｜"
             f"無資料 {len(nodata)} 檔{'（' + ','.join(nodata) + '）' if nodata else ''}"
             f"｜新增 {new_trades} 筆")

    if a.push:
        ts = datetime.now().strftime("%m-%d %H:%M")

        def git(*args, check=True):
            return subprocess.run(["git", *args], cwd=BASE,
                                  capture_output=True, text=True, check=check)
        git("add", "-f", "data/trades.db")
        git("add", "snapshot/index.html", "docs/index.html",
            "data/longshort_symbols.json")
        r = git("commit", "-m", f"paper step {ts}", check=False)
        if r.returncode == 0:
            git("pull", "--rebase", "--autostash", check=False)
            git("push")
            log.info("✓ 已推送（Vercel/Pages 自動部署）")
        else:
            log.info("（無變更，不推送）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
