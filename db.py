# -*- coding: utf-8 -*-
"""
sqlite3 交易紀錄儲存（學習導向）
================================
資料庫：data/trades.db（可用環境變數 FUBON_DB 覆寫路徑）

資料表：
  orders        — 真實下單紀錄（fubon_orders.py --send 每筆送單：測試/正式、成功/失敗）
  trades        — 交易明細（進出場時間價格、原因碼、結果標籤、特徵快照 JSON、損益、R 值）
  trade_events  — 逐事件流（TRIGGER/ENTRY/STOP_UPDATE/EXIT + 原因碼 + 詳情 JSON）
  stop_updates  — 移動停損軌跡（每根新長黑K的更新）
  signals_log   — 未成交易訊號（觸發被規則擋下/無下一根K）→ 學習「錯過」是否正確
  equity_curve  — 累計淨損益曲線
  pits          — 挖掘出的虧損模式（坑點：哪種情境勝率顯著低於基準）
  rules         — 避坑規則（核准後由策略引擎自動擋下符合條件的進場）
"""
import os
import sqlite3
from datetime import datetime

DB_PATH = os.environ.get("FUBON_DB") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "trades.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  mode TEXT, symbol TEXT, side TEXT, order_type TEXT,
  quantity INTEGER, price TEXT, price_type TEXT, market_type TEXT, tif TEXT, user_def TEXT,
  order_no TEXT, is_success INTEGER, message TEXT, raw TEXT
);
CREATE TABLE IF NOT EXISTS trades(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  symbol TEXT NOT NULL, date TEXT NOT NULL,
  direction TEXT,
  trigger_time TEXT, trigger_open REAL,
  entry_time TEXT, entry_price REAL,
  initial_stop REAL, final_stop REAL, stop_update_count INTEGER,
  exit_time TEXT, exit_reason TEXT, exit_price REAL,
  lots INTEGER, shares INTEGER,
  gross_pnl REAL, fees REAL, tax REAL, net_pnl REAL,
  risk_per_share REAL, r_multiple REAL, bars_held INTEGER,
  direction TEXT, is_dup INTEGER DEFAULT 0, entry_reason TEXT, result_tag TEXT, features TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS trade_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  trade_id INTEGER NOT NULL,
  seq INTEGER, ts TEXT, event TEXT, reason TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS stop_updates(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  trade_id INTEGER NOT NULL,
  seq INTEGER, bar_time TEXT, new_stop REAL, reason TEXT
);
CREATE TABLE IF NOT EXISTS signals_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  date TEXT, symbol TEXT, ts TEXT, event TEXT, reason TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS equity_curve(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL, date TEXT, cum_net_pnl REAL
);
CREATE TABLE IF NOT EXISTS pits(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT, dimension TEXT, bucket TEXT,
  n_trades INTEGER, n_loss INTEGER, loss_rate REAL, baseline_loss_rate REAL,
  avg_r REAL, verdict TEXT, suggestion TEXT
);
CREATE TABLE IF NOT EXISTS rules(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT, name TEXT, condition_json TEXT,
  action TEXT DEFAULT 'skip_entry', source TEXT DEFAULT 'manual',
  approved INTEGER DEFAULT 0, note TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_date ON trades(date);
CREATE INDEX IF NOT EXISTS idx_events_trade ON trade_events(trade_id);
"""

_ORDER_COLS = ("ts", "mode", "symbol", "side", "order_type", "quantity", "price",
               "price_type", "market_type", "tif", "user_def", "order_no",
               "is_success", "message", "raw")

_TRADE_COLS = ("run_id", "symbol", "date", "direction", "trigger_time", "trigger_open",
               "entry_time", "entry_price", "initial_stop", "final_stop",
               "stop_update_count", "exit_time", "exit_reason", "exit_price",
               "lots", "shares", "gross_pnl", "fees", "tax", "net_pnl",
               "risk_per_share", "r_multiple", "bars_held", "is_dup",
               "entry_reason", "result_tag", "features", "created_at")


def connect(path=None) -> sqlite3.Connection:
    """開啟連線並「自動確保資料表存在」（自癒：data/ 整個刪除也能自動復原架構）。
    SCHEMA 皆為 CREATE TABLE IF NOT EXISTS，冪等且開銷極小。"""
    p = path or DB_PATH
    d = os.path.dirname(p)
    if d:
        os.makedirs(d, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    except Exception as e:
        import logging
        logging.getLogger("fubon.db").debug(f"schema 確認略過：{e}")
    # 輕量遷移：舊 DB 補新欄位（已存在則忽略）
    for col in ("direction TEXT", "is_dup INTEGER DEFAULT 0"):
        try:
            conn.execute(f"ALTER TABLE trades ADD COLUMN {col}")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # 欄位已存在
    return conn


def init_db(conn: sqlite3.Connection):
    conn.executescript(SCHEMA)
    conn.commit()


def _now():
    return datetime.now().isoformat(timespec="seconds")


# ---------------- 真實下單 ----------------
def record_order(conn, **kw) -> int:
    kw.setdefault("ts", _now())
    cols = [c for c in _ORDER_COLS if c in kw]
    sql = f"INSERT INTO orders({','.join(cols)}) VALUES({','.join('?' * len(cols))})"
    cur = conn.execute(sql, [kw[c] for c in cols])
    conn.commit()
    return cur.lastrowid


# ---------------- 回測/交易（含事件流＋停損軌跡，一次寫入） ----------------
def get_blacklist(conn, min_trades=1):
    """回測虧損黑名單：累積回測淨損益為負的標的（排除合成 DEMO 樣本與重複標記）
    動態計算 — 資料累積後黑名單自動更新"""
    return [dict(r) for r in conn.execute(
        "SELECT symbol, COUNT(*) n, "
        "SUM(CASE WHEN net_pnl>0 THEN 1 ELSE 0 END) wins, "
        "SUM(net_pnl) net, AVG(r_multiple) avg_r, MAX(date) last_date "
        "FROM trades WHERE run_id NOT LIKE 'DEMO%' AND COALESCE(is_dup,0)=0 "
        "GROUP BY symbol HAVING SUM(net_pnl) < 0 AND COUNT(*) >= ? "
        "ORDER BY SUM(net_pnl) ASC", (min_trades,)).fetchall()]


def trade_exists(conn, t):
    """累積去重：同標的＋同日＋同進場時間＋同方向 視為同一筆（避免重複跑回測時雙重計入）"""
    row = conn.execute(
        "SELECT id FROM trades WHERE symbol=? AND date=? AND entry_time=? "
        "AND COALESCE(direction,'short')=COALESCE(?, 'short') LIMIT 1",
        (t.get("symbol"), t.get("date"), t.get("entry_time"),
         t.get("direction") or "short")).fetchone()
    return row is not None


def record_trade(conn, t: dict) -> int:
    row = {k: v for k, v in t.items() if k in _TRADE_COLS}
    # 累積去重：同標的＋同日＋同進場時間 已存在 → is_dup=1
    # （仍入庫供 run 報告完整顯示；學習統計 pits/黑名單排除 is_dup，避免雙重計入）
    row["is_dup"] = 1 if trade_exists(conn, t) else 0
    row.setdefault("created_at", _now())
    cols = [c for c in _TRADE_COLS if c in row]
    sql = f"INSERT INTO trades({','.join(cols)}) VALUES({','.join('?' * len(cols))})"
    cur = conn.execute(sql, [row[c] for c in cols])
    tid = cur.lastrowid
    for seq, ev in enumerate(t.get("events", []), 1):
        conn.execute("INSERT INTO trade_events(trade_id,seq,ts,event,reason,detail) "
                     "VALUES(?,?,?,?,?,?)",
                     (tid, seq, ev.get("ts"), ev.get("event"), ev.get("reason"),
                      ev.get("detail")))
    for seq, (bt, ns) in enumerate(t.get("stop_updates", []), 1):
        conn.execute("INSERT INTO stop_updates(trade_id,seq,bar_time,new_stop,reason) "
                     "VALUES(?,?,?,?,?)", (tid, seq, bt, ns, "S_STOP_UPDATE"))
    conn.commit()
    return tid


def record_signal(conn, date, symbol, ts, event, reason, detail=""):
    conn.execute("INSERT INTO signals_log(date,symbol,ts,event,reason,detail) "
                 "VALUES(?,?,?,?,?,?)", (date, symbol, ts, event, reason, detail))
    conn.commit()


def record_equity(conn, run_id, date, cum_net_pnl):
    conn.execute("INSERT INTO equity_curve(run_id,date,cum_net_pnl) VALUES(?,?,?)",
                 (run_id, date, cum_net_pnl))
    conn.commit()


# ---------------- 查詢 ----------------
def list_trades(conn, limit=20, run_id=None):
    if run_id:
        return conn.execute("SELECT * FROM trades WHERE run_id=? ORDER BY id DESC LIMIT ?",
                            (run_id, limit)).fetchall()
    return conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def get_trade(conn, trade_id):
    return conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()


def list_events(conn, trade_id):
    return conn.execute("SELECT * FROM trade_events WHERE trade_id=? ORDER BY seq",
                        (trade_id,)).fetchall()


def list_stop_updates(conn, trade_id):
    return conn.execute("SELECT * FROM stop_updates WHERE trade_id=? ORDER BY seq",
                        (trade_id,)).fetchall()


def count_orders(conn):
    return conn.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"]


# ---------------- 學習迴路：坑點與規則 ----------------
def find_rule_by_condition(conn, condition_json):
    return conn.execute("SELECT * FROM rules WHERE condition_json=?",
                        (condition_json,)).fetchone()


def delete_rule(conn, rule_id):
    conn.execute("DELETE FROM rules WHERE id=?", (rule_id,))
    conn.commit()


def save_pit(conn, dimension, bucket, n_trades, n_loss, loss_rate,
             baseline_loss_rate, avg_r, verdict, suggestion) -> int:
    """坑點入庫（upsert：同維度＋分組以最新證據更新，不重複）"""
    bucket = str(bucket)
    existing = conn.execute("SELECT id FROM pits WHERE dimension=? AND bucket=?",
                            (dimension, bucket)).fetchone()
    if existing:
        conn.execute(
            "UPDATE pits SET created_at=?, n_trades=?, n_loss=?, loss_rate=?, "
            "baseline_loss_rate=?, avg_r=?, verdict=?, suggestion=? WHERE id=?",
            (_now(), n_trades, n_loss, loss_rate, baseline_loss_rate, avg_r,
             verdict, suggestion, existing["id"]))
        conn.commit()
        return existing["id"]
    cur = conn.execute(
        "INSERT INTO pits(created_at,dimension,bucket,n_trades,n_loss,loss_rate,"
        "baseline_loss_rate,avg_r,verdict,suggestion) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (_now(), dimension, bucket, n_trades, n_loss, loss_rate,
         baseline_loss_rate, avg_r, verdict, suggestion))
    conn.commit()
    return cur.lastrowid


def get_pit(conn, pit_id):
    return conn.execute("SELECT * FROM pits WHERE id=?", (pit_id,)).fetchone()


def list_pits(conn, limit=50):
    return conn.execute("SELECT * FROM pits ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def save_rule(conn, name, condition_json, source="manual", note="") -> int:
    cur = conn.execute(
        "INSERT INTO rules(created_at,name,condition_json,action,source,approved,note) "
        "VALUES(?,?,?,?,?,0,?)", (_now(), name, condition_json, "skip_entry", source, note))
    conn.commit()
    return cur.lastrowid


def approve_rule(conn, rule_id, approved=1):
    conn.execute("UPDATE rules SET approved=? WHERE id=?", (approved, rule_id))
    conn.commit()


def list_rules(conn, approved_only=False):
    if approved_only:
        return conn.execute("SELECT * FROM rules WHERE approved=1 ORDER BY id").fetchall()
    return conn.execute("SELECT * FROM rules ORDER BY id").fetchall()
