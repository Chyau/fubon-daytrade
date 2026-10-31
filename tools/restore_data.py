# -*- coding: utf-8 -*-
"""
一鍵復原 data/ 目錄
====================
用法：python tools/restore_data.py [--no-chips] [--skip-scanner]
步驟：備份既有 DB → 重建資料表 → 2426 案例 CSV → 掃描器清單 → 籌碼快取 → 今日觀察清單
"""
import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from logutil import get_logger, ensure_utf8_stdout  # noqa: E402

ensure_utf8_stdout()
log = get_logger("restore")


def main():
    ap = argparse.ArgumentParser(description="一鍵復原 data/ 目錄（結構＋快取＋清單全重建）")
    ap.add_argument("--no-chips", action="store_true", help="跳過籌碼快取下載（約 7MB）")
    ap.add_argument("--skip-scanner", action="store_true", help="跳過掃描器清單抓取")
    a = ap.parse_args()
    import db as tdb       # noqa: E402
    import chips as chips_mod  # noqa: E402

    data_dir = os.path.join(BASE, "data")
    backup_dir = os.path.join(data_dir, "backup")
    os.makedirs(backup_dir, exist_ok=True)
    results = []

    # 1) 備份既有 DB（復原前先保護歷史資料）
    dbp = os.path.join(data_dir, "trades.db")
    if os.path.exists(dbp):
        bak = os.path.join(backup_dir, f"trades_{datetime.now():%Y%m%d_%H%M%S}.db")
        shutil.copy2(dbp, bak)
        results.append(("備份既有 DB", bak))
    else:
        results.append(("備份既有 DB", "（無既有 DB，略過）"))

    # 2) DB 架構自動重建（connect 自癒）
    try:
        conn = tdb.connect()
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        n = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        conn.close()
        results.append(("資料表架構", f"{len(tables)} 張表（trades 現有 {n} 筆）"))
    except Exception as e:
        results.append(("資料表架構", f"✗ {str(e)[:80]}"))

    # 3) 2426 案例 CSV
    try:
        r = subprocess.run([sys.executable, os.path.join(BASE, "tools", "gen_2426_case.py")],
                           capture_output=True, text=True, encoding="utf-8")
        results.append(("2426 案例 CSV", "✓ 已重建" if r.returncode == 0
                        else f"✗ {str(r.stderr)[:80]}"))
    except Exception as e:
        results.append(("2426 案例 CSV", f"✗ {str(e)[:80]}"))

    # 4) 掃描器清單
    if a.skip_scanner:
        results.append(("掃描器清單", "略過（--skip-scanner）"))
    else:
        try:
            total, hot, updated = chips_mod.fetch_scanner_candidates(log=log)
            results.append(("掃描器清單", f"{total} 支（推薦 {hot}，更新 {updated}）"))
        except Exception as e:
            results.append(("掃描器清單", f"✗ {str(e)[:80]}"))

    # 5) 籌碼快取
    if a.no_chips:
        results.append(("籌碼快取", "略過（--no-chips；首次造訪 🧲 頁會自動下載）"))
    else:
        try:
            D = chips_mod.load_dataset(force=True)
            results.append(("籌碼快取", f"✓ {len(D['stocks'])} 檔（最新 {D['dates'][-1]}）"))
        except Exception as e:
            results.append(("籌碼快取", f"✗ {str(e)[:80]}"))

    # 6) 今日觀察清單
    try:
        D = chips_mod.load_dataset()
        out, lines = chips_mod.build_watchlist(D, min_branch=30)
        results.append(("今日觀察清單", f"{os.path.basename(out)}（{len(lines)} 檔）"))
    except Exception as e:
        results.append(("今日觀察清單", f"✗ {str(e)[:80]}"))

    print("=" * 62)
    print("🧰 data/ 一鍵復原完成")
    print("=" * 62)
    for k, v in results:
        print(f"  {k:<14} {v}")
    print("-" * 62)
    print("提醒：若網頁伺服器正在執行，請重啟（Ctrl+C → start_web.bat）以釋放舊檔案鎖。")
    try:
        import notify
        notify.send("🧰 data/ 一鍵復原完成\n" + "\n".join(f"{k}: {v}" for k, v in results))
    except Exception:
        pass


if __name__ == "__main__":
    main()
