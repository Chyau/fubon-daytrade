# -*- coding: utf-8 -*-
"""整合主檔產生器 — 系統數據＋權證小哥數據 → 一個完整的 SQLite 檔案

輸出：data/integrated_master.db
  ① 佛系當沖系統（data/trades.db 全部表）
  ② 權證小哥 tw_stock.db 全部表（50 張；🔎 Mr.Price 當沖系統的 6 張表
     daytrade_stocks/login_sessions/meta/sdk_events/trade_details/trading_tasks
     **不在來源 DB 中** — 已確認，無資料可整合）
  ③ twdata JSON 快照（9 個）→ source_json 表
  ④ integration_manifest：來源/表名/列數/整合時間

用法：python tools/integrate_master.py   （可重複執行＝重建）
"""
import io
import os
import sqlite3
import sys
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)

SRC_SYSTEM = os.path.join(PROJ, "data", "trades.db")
SRC_TWDB = r"D:\權證小哥-當沖飆股神手\twdata\tw_stock.db"
SRC_TWJSON = r"D:\權證小哥-當沖飆股神手\twdata"
OUT = os.path.join(PROJ, "data", "integrated_master.db")

MR_PRICE_TABLES = {"daytrade_stocks", "login_sessions", "meta", "sdk_events",
                   "trade_details", "trading_tasks"}
JSON_FILES = ["sentiment.json", "chips_daily.json", "screener_rows.json",
              "us_scan.json", "us_narrative.json", "groups.json",
              "deriv_futures.json", "warrants_trim.json",
              "deriv_warrants_full.json"]


def copy_table(src, dst, table):
    """跨檔複製一張表（schema＋資料）。回傳列數。"""
    scol = sqlite3.connect(src)
    scol.row_factory = sqlite3.Row
    try:
        ddl = scol.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table,)).fetchone()[0]
        cols = [r[1] for r in scol.execute(f"PRAGMA table_info([{table}])")]
        rows = scol.execute(f"SELECT * FROM [{table}]").fetchall()
    finally:
        scol.close()
    dst.execute(f'DROP TABLE IF EXISTS "{table}"')
    dst.execute(ddl)
    if rows:
        ph = ",".join("?" for _ in cols)
        cl = ",".join(f'"{c}"' for c in cols)
        dst.executemany(f'INSERT INTO "{table}" ({cl}) VALUES ({ph})',
                        [tuple(r) for r in rows])
    return len(rows)


def main():
    for p in (SRC_SYSTEM, SRC_TWDB):
        if not os.path.exists(p):
            print(f"✗ 來源不存在：{p}")
            return
    if os.path.exists(OUT):
        os.remove(OUT)
        print("（移除舊檔重建）")
    dst = sqlite3.connect(OUT)
    manifest = []

    print("── ① 佛系當沖系統（trades.db）──")
    con = sqlite3.connect(SRC_SYSTEM)
    sys_tables = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()]
    con.close()
    for t in sys_tables:
        n = copy_table(SRC_SYSTEM, dst, t)
        manifest.append(("佛系當沖系統", t, n))
        print(f"  {t}: {n:,} rows")

    print("── ② 權證小哥（tw_stock.db）──")
    con = sqlite3.connect(SRC_TWDB)
    tw_tables = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()]
    con.close()
    excluded = [t for t in tw_tables if t in MR_PRICE_TABLES]
    for t in tw_tables:
        if t in MR_PRICE_TABLES:
            print(f"  [排除] {t}（🔎 Mr.Price 當沖系統）")
            continue
        n = copy_table(SRC_TWDB, dst, t)
        manifest.append(("權證小哥tw_stock", t, n))
        print(f"  {t}: {n:,} rows")

    print("── ③ JSON 快照（twdata）──")
    dst.execute("CREATE TABLE IF NOT EXISTS source_json("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, "
                "bytes INTEGER, content TEXT)")
    for fn in JSON_FILES:
        p = os.path.join(SRC_TWJSON, fn)
        if not os.path.exists(p):
            print(f"  [略過] {fn}（不存在）")
            continue
        content = open(p, encoding="utf-8", errors="replace").read()
        dst.execute("INSERT INTO source_json(name, bytes, content) VALUES (?,?,?)",
                    (fn, len(content.encode("utf-8")), content))
        manifest.append(("JSON快照", fn, len(content)))
        print(f"  {fn}: {len(content):,} bytes")

    dst.execute("CREATE TABLE IF NOT EXISTS integration_manifest("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, "
                "item TEXT, rows INTEGER, integrated_at TEXT)")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    dst.executemany("INSERT INTO integration_manifest(source, item, rows, "
                    "integrated_at) VALUES (?,?,?,?)",
                    [(s, i, n, now) for s, i, n in manifest])
    dst.commit()

    total_rows = sum(n for _, _, n in manifest)
    size = os.path.getsize(OUT)
    print(f"\n✓ 整合完成：{OUT}")
    print(f"  大小 {size/1024/1024:.1f} MB｜{len(manifest)} 個資料項｜總列數 {total_rows:,}")
    print("  🔎 Mr.Price 當沖系統："
          + (f"已排除 {excluded}" if excluded
             else "來源 DB 內無 Mr.Price 表（確認無該系統資料混入）"))
    dst.close()


if __name__ == "__main__":
    main()
