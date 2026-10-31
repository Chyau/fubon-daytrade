# -*- coding: utf-8 -*-
r"""
每日更新（建議每個交易日 17:30 執行）
======================================
1. 備份 trades.db → data/backup/
2. 籌碼快取刷新（取得新交易日資料）
3. 掃描器清單重抓（網站每日 17:00 後更新）
4. 產生「明日」觀察清單
5. Telegram 摘要（已設定時）

排程註冊（管理員 PowerShell 執行一次即可）：
  schtasks /Create /TN "FubonDaytradeDailyUpdate" /SC DAILY /ST 17:30 ^
    /TR "cmd /c C:\Users\Chyau\Documents\預設專案\fubon-daytrade\daily_update.bat"
"""
import os
import sys
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from logutil import get_logger, ensure_utf8_stdout  # noqa: E402

ensure_utf8_stdout()
log = get_logger("daily")


def main():
    import db as tdb            # noqa: E402
    import chips as chips_mod   # noqa: E402
    results = []

    # 1) 備份 DB
    backup_dir = os.path.join(BASE, "data", "backup")
    os.makedirs(backup_dir, exist_ok=True)
    dbp = os.path.join(BASE, "data", "trades.db")
    if os.path.exists(dbp):
        bak = os.path.join(backup_dir, f"trades_{datetime.now():%Y%m%d}.db")
        import shutil
        shutil.copy2(dbp, bak)
        results.append(("DB 備份", os.path.basename(bak)))
    else:
        results.append(("DB 備份", "無 DB（略過）"))

    # 2) 籌碼快取刷新
    try:
        D = chips_mod.load_dataset(force=True)
        results.append(("籌碼快取", f"{len(D['stocks'])} 檔（最新 {D['dates'][-1]}）"))
    except Exception as e:
        results.append(("籌碼快取", f"✗ {str(e)[:80]}"))
        D = None

    # 3) 掃描器清單
    try:
        total, hot, updated = chips_mod.fetch_scanner_candidates(log=log)
        results.append(("掃描器清單", f"{total} 支（推薦 {hot}，{updated}）"))
    except Exception as e:
        results.append(("掃描器清單", f"✗ {str(e)[:80]}"))

    # 4) 明日觀察清單
    codes = []
    if D:
        try:
            out, lines = chips_mod.build_watchlist(D, min_branch=30)
            results.append(("明日觀察清單", f"{os.path.basename(out)}（{len(lines)} 檔）"))
        except Exception as e:
            results.append(("明日觀察清單", f"✗ {str(e)[:80]}"))

    # 5) 每日回測累積（學習迴路養分：觀察清單 × 富邦當日 5 分K → 自動入庫，重複自動略過）
    import glob as _glob
    wls = sorted(_glob.glob(os.path.join(BASE, "data", "watchlist_*.txt")))
    if wls:
        for ln in open(wls[-1], encoding="utf-8"):
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                codes.append(ln.split()[0])
    codes = codes[:30]
    new_trades = dup = errors = 0
    if not codes:
        results.append(("每日回測累積", "無觀察清單（先跑 chips.py --watchlist）"))
    else:
        try:
            import backtest as bt            # noqa: E402
            import strategy as strategy_mod  # noqa: E402
            import fubon_orders as fo        # noqa: E402
            sdk, acc = fo.connect_sdk(fo.fubon_cfg())
            try:
                sdk.init_realtime()
            except Exception:
                pass
            cfg = {**strategy_mod.DEFAULTS}
            total0 = conn = None
            c2 = tdb.connect()
            total0 = c2.execute("SELECT COUNT(*) c FROM trades").fetchone()["c"]
            c2.close()
            done_dates = set()
            for sym in codes:
                try:
                    bars = fo.fetch_intraday_bars(sdk, sym)
                    if not bars:
                        continue
                    d = bars[0]["date"]
                    if d in done_dates:
                        continue
                    done_dates.add(d)
                    bt.run_backtest(bars, cfg, sym, f"DAILY-{d}", log)
                except Exception as e:
                    errors += 1
                    log.warning(f"[{sym}] 每日回測失敗：{str(e)[:80]}")
            try:
                sdk.logout()
            except Exception:
                pass
            c2 = tdb.connect()
            total1 = c2.execute("SELECT COUNT(*) c FROM trades").fetchone()["c"]
            c2.close()
            results.append(("每日回測累積", f"{len(codes)} 檔／{len(done_dates)} 日｜"
                             f"新增 {total1 - total0} 筆（失敗 {errors}）｜總計 {total1} 筆"))
        except Exception as e:
            results.append(("每日回測累積", f"✗ {str(e)[:80]}（登入或網路問題）"))

    print("=" * 62)
    print(f"📅 每日更新完成 {datetime.now():%Y-%m-%d %H:%M}")
    print("=" * 62)
    for k, v in results:
        print(f"  {k:<14} {v}")
    try:
        import notify
        notify.send("📅 每日更新完成\n" + "\n".join(f"{k}: {v}" for k, v in results))
    except Exception:
        pass


if __name__ == "__main__":
    main()
