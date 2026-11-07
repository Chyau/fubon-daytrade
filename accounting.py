# -*- coding: utf-8 -*-
"""帳務模組 — 庫存查詢、損益、銀行餘額、現股/零股手動下單

SDK 2.2.8 的帳務 API 在 sdk.accounting（2026-11-06 實測確認）：
  inventories(acc)                    庫存（整股+零股餘額分開；無買進日期/均價）
  unrealized_gains_and_loses(acc)     未實現損益（cost_price/profit/loss）
  realized_gains_and_loses(acc)       已實現損益明細
  bank_remain(acc)                    銀行餘額
  （買進日期/買進均價由 sdk.stock.filled_history 的買進成交補）

手動下單：
  現股整張  MarketType.Common      qty 為 1000 的倍數
  盤中零股  MarketType.IntradayOdd  qty 1~999 股（零股不可當沖、不可融資融券）
  零股 tick 與整股不同（tick_size_odd）— 限價需對齊，否則後檯拒單
安全：PLACE_ORDER=false（預設）時真實送單一律被擋，只允許 dry 模擬記錄。
"""
import os

import db as tdb
from logutil import get_logger

log = get_logger("accounting")


def _f(v):
    """安全轉 float（SDK 物件屬性/None 混雜）"""
    try:
        return float(v) if v not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


def _i(v):
    try:
        return int(v) if v not in (None, "") else 0
    except (TypeError, ValueError):
        return 0


def tick_size_odd(px):
    """盤中零股 tick 級距（與整股不同：50 元以下 0.01）"""
    if px < 50:
        return 0.01
    if px < 100:
        return 0.05
    if px < 500:
        return 0.1
    if px < 1000:
        return 0.5
    return 1.0


def bank_remain(sdk, acc):
    """銀行餘額 → {"currency","balance"} 或 None
    ⚠沙箱（測試環境）呼叫會使 SDK Rust 核心 panic 並毒化內部鎖
    （後續所有帳務呼叫連環失敗）→ 測試環境直接跳過，正式環境才查。
    panic 後自動 reset_sdk_session() 恢復。"""
    import fubon_orders as fo
    if fo.fubon_cfg()["use_test"]:
        return {"currency": "TWD", "balance": 0.0,
                "note": "測試環境不支援銀行餘額查詢"}
    try:
        res = sdk.accounting.bank_remain(acc)
        d = getattr(res, "data", None)
        if isinstance(d, list):
            d = d[0] if d else None
        return {"currency": getattr(d, "currency", None) or "TWD",
                "balance": _f(getattr(d, "balance", 0))}
    except BaseException as e:
        log.warning(f"bank_remain 失敗：{str(e)[:120]} → 重置 SDK session")
        try:
            fo.reset_sdk_session()
        except Exception:
            pass
        return None


def fo_dig(res):
    """Response 物件 → {"is_success","message","data"}（data 可為 list/obj）"""
    return {"is_success": getattr(res, "is_success", None),
            "message": getattr(res, "message", None),
            "data": getattr(res, "data", None)}


def _recover(e):
    """SDK Rust panic／mutex 毒化後重置 session（下次呼叫自動重新登入）"""
    s = str(e)
    if "PanicException" in type(e).__name__ or "PoisonError" in s or "unwrap" in s:
        try:
            import fubon_orders as fo
            fo.reset_sdk_session()
            log.warning(f"SDK session 已重置（{s[:80]}）")
        except Exception:
            pass


def inventories(sdk, acc):
    """庫存 → [{symbol, qty_whole, qty_odd, qty_total, tradable, buy_value, buy_avg}]
    buy_avg＝買進價金÷買進成交股數（後檯累計值，非本筆均價）"""
    out = []
    try:
        res = sdk.accounting.inventories(acc)
        for it in (getattr(res, "data", None) or []):
            odd = getattr(it, "odd", None)
            qty_w = _i(getattr(it, "today_qty", 0))
            qty_o = _i(getattr(odd, "today_qty", 0)) if odd else 0
            buy_filled = _i(getattr(it, "buy_filled_qty", 0)) + \
                (_i(getattr(odd, "buy_filled_qty", 0)) if odd else 0)
            buy_value = _f(getattr(it, "buy_value", 0)) + \
                (_f(getattr(odd, "buy_value", 0)) if odd else 0)
            if qty_w + qty_o == 0:
                continue   # 零庫存雜訊列（後檯會列出已清倉的委託類型）
            out.append({
                "symbol": getattr(it, "stock_no", ""),
                "qty_whole": qty_w, "qty_odd": qty_o,
                "qty_total": qty_w + qty_o,
                "tradable": _i(getattr(it, "tradable_qty", 0)),
                "buy_value": buy_value,
                "buy_avg": round(buy_value / buy_filled, 2) if buy_filled else None,
                "order_type": str(getattr(it, "order_type", "")),
            })
    except BaseException as e:
        log.warning(f"inventories 失敗：{e}")
    return out


def unrealized(sdk, acc):
    """未實現損益 → [{symbol, cost_price, qty, profit, loss, net}]"""
    out = []
    try:
        res = sdk.accounting.unrealized_gains_and_loses(acc)
        for it in (getattr(res, "data", None) or []):
            profit = _f(getattr(it, "unrealized_profit", 0))
            loss = _f(getattr(it, "unrealized_loss", 0))
            out.append({
                "symbol": getattr(it, "stock_no", ""),
                "cost_price": _f(getattr(it, "cost_price", 0)),
                "qty": _i(getattr(it, "tradable_qty", 0)),
                "profit": profit, "loss": loss,
                "net": round(profit - loss, 0),
            })
    except BaseException as e:
        _recover(e)
        log.warning(f"unrealized 失敗：{e}")
    return out


def realized(sdk, acc):
    """已實現損益 → {"rows":[...], "summary":{"profit","loss","net"}}"""
    rows = []
    try:
        res = sdk.accounting.realized_gains_and_loses(acc)
        tot_p = tot_l = 0.0
        for it in (getattr(res, "data", None) or []):
            profit = _f(getattr(it, "realizedProfit", 0))
            loss = _f(getattr(it, "realizedLoss", 0))
            tot_p += profit
            tot_l += loss
            rows.append({
                "date": getattr(it, "date", ""),
                "symbol": getattr(it, "stockNo", "") or getattr(it, "stock_no", ""),
                "buy_sell": str(getattr(it, "buySell", "") or getattr(it, "buy_sell", "")),
                "qty": _i(getattr(it, "filledQty", 0) or getattr(it, "filled_qty", 0)),
                "price": _f(getattr(it, "filledPrice", 0) or getattr(it, "filled_price", 0)),
                "profit": profit, "loss": loss,
                "net": round(profit - loss, 0),
            })
        return {"rows": rows, "summary": {
            "profit": round(tot_p, 0), "loss": round(tot_l, 0),
            "net": round(tot_p - tot_l, 0)}}
    except BaseException as e:
        _recover(e)
        log.warning(f"realized 失敗：{e}")
        return {"rows": [], "summary": {"profit": 0, "loss": 0, "net": 0}}


def buy_fills(sdk, acc, symbol=None, limit=200):
    """買進成交明細（買進日期/價格來源）→ [{date, symbol, price, qty, time}]"""
    out = []
    try:
        res = sdk.stock.filled_history(acc)
        for it in (getattr(res, "data", None) or []):
            if str(getattr(it, "buy_sell", "")) != "BSAction.Buy":
                continue
            sym = getattr(it, "stock_no", "")
            if symbol and sym != symbol:
                continue
            out.append({"date": getattr(it, "date", ""),
                        "symbol": sym,
                        "price": _f(getattr(it, "filled_avg_price", 0)),
                        "qty": _i(getattr(it, "filled_qty", 0)),
                        "time": getattr(it, "filled_time", "")})
            if len(out) >= limit:
                break
    except BaseException as e:
        _recover(e)
        log.debug(f"filled_history 失敗：{e}")
    return out


def inventories_with_buyinfo(sdk, acc):
    """庫存＋買進日期（由 filled_history 最近一筆買進推得）"""
    inv = inventories(sdk, acc)
    fills = buy_fills(sdk, acc)
    last_buy = {}
    for f in fills:
        if f["symbol"] not in last_buy:   # filled_history 依時序 → 第一筆即最近
            last_buy[f["symbol"]] = f
    for it in inv:
        f = last_buy.get(it["symbol"])
        it["last_buy_date"] = f["date"] if f else None
        it["last_buy_price"] = f["price"] if f else it.get("buy_avg")
    return inv


def place_manual(sdk, acc, symbol, side, qty, price=None, odd=False, dry=True,
                 user_def="MANUAL", mode_tag="manual"):
    """現股手動下單（整張/零股）。
    dry=True：只記錄不送單（安全預設）。真送單需 PLACE_ORDER=true。
    回傳 {"ok", "message", "order_no", "recorded"}"""
    symbol = str(symbol or "").strip()
    side = "buy" if str(side).lower().startswith("b") else "sell"
    qty = _i(qty)
    if not symbol or qty <= 0:
        return {"ok": False, "message": "標的與數量必填", "order_no": None}
    if odd:
        if qty >= 1000:
            return {"ok": False, "message": "零股數量需 1~999 股（整張請關閉零股模式）",
                    "order_no": None}
    else:
        if qty % 1000 != 0:
            return {"ok": False, "message": "整張數量需為 1000 的倍數"
                    "（零股請開啟零股模式）", "order_no": None}
    px = None
    is_mkt = not price
    if price:
        try:
            px = round(float(price), 2)
        except (TypeError, ValueError):
            return {"ok": False, "message": "限價格式錯誤", "order_no": None}
        if not is_mkt:
            t = tick_size_odd(px) if odd else _tick_whole(px)
            if abs(round(px / t) * t - px) > 1e-9:
                return {"ok": False,
                        "message": f"限價 {px} 不符合 tick（{'零股' if odd else '整股'} "
                                   f"{t}）", "order_no": None}

    real = (not dry) and os.environ.get("PLACE_ORDER", "false").strip().lower() == "true"
    note = "dry 模擬" if dry else ("真實送單" if real else "被擋（PLACE_ORDER=false）")

    order_no, ok, msg = None, False, note
    if real:
        try:
            from fubon_neo.sdk import Order
            from fubon_neo.constant import (BSAction, MarketType, PriceType,
                                            TimeInForce, OrderType)
            order = fo_build_order(Order, BSAction, MarketType, PriceType,
                                   TimeInForce, OrderType, symbol, side, qty,
                                   px, is_mkt, odd, "ROD", "stock", user_def)
            res = sdk.stock.place_order(acc, order)
            ok = bool(getattr(res, "is_success", False))
            msg = getattr(res, "message", None)
            data = getattr(res, "data", None)
            order_no = getattr(data, "order_no", None) if data else None
        except BaseException as e:
            ok, msg = False, str(e)[:200]

    conn = tdb.connect()
    try:
        tdb.record_order(conn, mode=mode_tag, symbol=symbol, side=side,
                         order_type="stock", quantity=qty,
                         price=str(px) if px is not None else "",
                         price_type="Limit" if px is not None else "Market",
                         market_type="IntradayOdd" if odd else "Common",
                         tif="ROD", user_def=user_def, order_no=order_no,
                         is_success=1 if (dry or ok) else 0,
                         message=f"manual {note}｜{msg or ''}", raw="")
    finally:
        conn.close()
    return {"ok": ok if real else False, "message": f"{note}｜{msg or '已記錄'}",
            "order_no": order_no, "recorded": True}


def _tick_whole(px):
    if px < 10:
        return 0.01
    if px < 50:
        return 0.05
    if px < 100:
        return 0.1
    if px < 500:
        return 0.5
    if px < 1000:
        return 1.0
    return 5.0


def fo_build_order(*args):
    """延遲匯入避免循環（fubon_orders 較重）"""
    import fubon_orders as fo
    return fo.build_order(*args)
