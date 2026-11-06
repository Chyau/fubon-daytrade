# -*- coding: utf-8 -*-
"""跨日持倉管理 — 波段/定期定額的部位帳本（DB 持久化，重啟不消失）

策略代號：
  lu_swing   漲停次日波段（停損＝漲停日收盤價＝跌回即出；持有 ≤ max_hold_days）
  sig_swing  訊號波段版（當沖做多訊號進場，停損＝觸發K低點，持有 ≤ max_hold_days）
  dca        定期定額（無自動出場，人工管理）
  manual     帳務頁手動下單建立的持倉

停損檢查：即時價（intraday.trades 最後成交）≤ stop → 市價賣出平倉。
持有天數：日曆日計（保守）；達上限 → 13:20 後出場（X_HOLD_EXPIRE）。
真實送單閘門：PLACE_ORDER=true（同全系統總開關）。
"""
import os
import sqlite3
from datetime import datetime

sys_path = os.path.dirname(os.path.abspath(__file__))
import sys as _sys
if sys_path not in _sys.path:
    _sys.path.insert(0, sys_path)

import db as tdb
from logutil import get_logger, ensure_utf8_stdout

ensure_utf8_stdout()
log = get_logger("positions")


def open_position(symbol, strategy, qty, buy_price, stop, max_hold_days=3,
                  note="", dry=False):
    """建立持倉紀錄。回傳 position id。"""
    conn = tdb.connect()
    try:
        cur = conn.execute(
            "INSERT INTO positions(open_date, symbol, strategy, qty, buy_price, stop, "
            "max_hold_days, status, note, created_at) "
            "VALUES (?,?,?,?,?,?,?, 'open', ?, datetime('now','localtime'))",
            (datetime.now().strftime("%Y-%m-%d"), symbol, strategy, qty,
             buy_price, stop, max_hold_days,
             (note + ("（dry）" if dry else ""))))
        conn.commit()
        pid = cur.lastrowid
    finally:
        conn.close()
    log.info(f"[持倉] 開倉 #{pid} {symbol} {strategy} {qty} 股 @ {buy_price}"
             f"｜停損 {stop}｜最長 {max_hold_days} 日{'（dry）' if dry else ''}")
    return pid


def list_open(strategy=None):
    conn = tdb.connect()
    conn.row_factory = sqlite3.Row
    try:
        if strategy:
            rows = conn.execute(
                "SELECT * FROM positions WHERE status='open' AND strategy=? "
                "ORDER BY id", (strategy,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM positions WHERE status='open' ORDER BY id").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def close_position(pid, close_price, reason="", dry=False):
    """平倉並結算損益。"""
    conn = tdb.connect()
    conn.row_factory = sqlite3.Row
    try:
        r = conn.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone()
        if not r or r["status"] != "open":
            return False
        pnl = round((float(close_price) - r["buy_price"]) * r["qty"], 0)
        conn.execute(
            "UPDATE positions SET status='closed', close_date=?, close_price=?, "
            "pnl=?, note=? WHERE id=?",
            (datetime.now().strftime("%Y-%m-%d"), close_price, pnl,
             (r["note"] or "") + f"｜出場:{reason}" + ("（dry）" if dry else ""), pid))
        conn.commit()
        log.info(f"[持倉] 平倉 #{pid} {r['symbol']} {r['qty']} 股 @ {close_price}"
                 f"｜損益 {pnl:+,.0f}（{reason}）{'（dry）' if dry else ''}")
        return True
    finally:
        conn.close()


def update_stop(pid, new_stop):
    conn = tdb.connect()
    try:
        conn.execute("UPDATE positions SET stop=? WHERE id=? AND status='open'",
                     (new_stop, pid))
        conn.commit()
    finally:
        conn.close()


def hold_days(open_date, today=None):
    """開倉日到今天經過的日曆日數（保守：最長持有會提早於交易日數出場）"""
    today = today or datetime.now().strftime("%Y-%m-%d")
    try:
        d0 = datetime.strptime(str(open_date)[:10], "%Y-%m-%d")
        d1 = datetime.strptime(str(today)[:10], "%Y-%m-%d")
        return (d1 - d0).days
    except Exception:
        return 0


def last_price(sdk, symbol):
    """即時參考價：intraday.trades 最後成交；失敗回 None"""
    try:
        import fubon_orders as fo
        md = fo.ensure_realtime(sdk)
        if md is None:
            return None
        ticks = fo.fetch_intraday_trades(sdk, symbol, limit=5) or []
        if ticks:
            return float(ticks[-1].get("price") or 0) or None
    except BaseException as e:
        log.debug(f"last_price 失敗：{str(e)[:80]}")
    return None


def sdk_acc(sdk):
    """從已登入 SDK 取第一個證券帳戶"""
    for a in (sdk.list_accounts() or []):
        if getattr(a, "account_type", "") == "stock":
            return a
    return (sdk.list_accounts() or [None])[0]


def _place(sdk, symbol, side, qty, odd, dry, tag="SWING"):
    """市價送單（真實需 PLACE_ORDER=true）。回傳 (ok, msg, order_no)"""
    real = (not dry) and os.environ.get("PLACE_ORDER", "false").strip().lower() == "true"
    if not real:
        return False, "dry 模擬（未送單）", None
    try:
        import fubon_orders as fo
        from fubon_neo.sdk import Order
        from fubon_neo.constant import (BSAction, MarketType, PriceType,
                                        TimeInForce, OrderType)
        order = fo.build_order(Order, BSAction, MarketType, PriceType,
                               TimeInForce, OrderType, symbol, side, qty,
                               None, True, odd, "ROD", "stock", tag)
        res = sdk.stock.place_order(sdk_acc(sdk), order)
        ok = bool(getattr(res, "is_success", False))
        msg = getattr(res, "message", None)
        ono = getattr(getattr(res, "data", None), "order_no", None)
        conn = tdb.connect()
        try:
            tdb.record_order(conn, mode="swing", symbol=symbol, side=side,
                             order_type="stock", quantity=qty, price="",
                             price_type="Market", market_type="Common", tif="ROD",
                             user_def=tag, order_no=str(ono) if ono else None,
                             is_success=int(ok),
                             message=f"{tag} {side}｜{msg or ''}", raw="")
        finally:
            conn.close()
        return ok, (msg or "已送單"), ono
    except BaseException as e:
        log.warning(f"[持倉] 送單例外：{str(e)[:150]}")
        return False, str(e)[:150], None


def check_exits(sdk, dry=True):
    """巡所有未平倉部位：停損觸發／持有到期 → 賣出平倉。
    回傳處理清單 [{pid, symbol, action, price, sent}]"""
    actions = []
    today = datetime.now().strftime("%Y-%m-%d")
    now_hhmm = datetime.now().strftime("%H:%M")
    for p in list_open():
        if p["strategy"] == "dca":
            continue   # 定期定額不自動出場
        px = last_price(sdk, p["symbol"])
        if not px:
            continue
        reason = None
        if p["stop"] and px <= p["stop"]:
            reason = "X_STRUCT_STOP"
        elif (p["max_hold_days"] and
              hold_days(p["open_date"], today) >= p["max_hold_days"]
              and now_hhmm >= "13:20"):
            reason = "X_HOLD_EXPIRE"
        if not reason:
            continue
        qty = p["qty"]
        ok, msg, ono = _place(sdk, p["symbol"], "sell", qty, qty < 1000, dry)
        if ok or dry:
            close_position(p["id"], px, reason, dry=dry)
        actions.append({"pid": p["id"], "symbol": p["symbol"],
                        "action": reason, "price": px, "sent": ok, "msg": msg})
    return actions


if __name__ == "__main__":
    print("現有未平倉：")
    for p in list_open():
        print(" ", p)
