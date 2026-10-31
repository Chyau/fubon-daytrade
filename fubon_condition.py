# -*- coding: utf-8 -*-
"""
fubon_condition.py — 條件單（Smart Condition）串接
====================================================
佛系當沖搭配用法：
  ① --stop  進場放空後下「停損買回條件單」：股價 > 停損價 → 自動現股買進（當日有效）
  ② --entry 進場觸發當沖條件單：觸發價 → 送出父單，13:20 前未反向平倉 → 自動市價回補
     （引擎掛掉也有保護；--entry 的父單＋自動回補＝完整當沖條件單）

指令：
  python fubon_condition.py --list
  python fubon_condition.py --cancel GUID
  python fubon_condition.py --stop 2330 --qty 1000 --trigger 2380.0
  python fubon_condition.py --entry 2330 --qty 1000 --trigger 2400 --side sell [--cmp gt|lt] [--end 132000]
"""
import argparse

from logutil import get_logger, ensure_utf8_stdout

ensure_utf8_stdout()
log = get_logger("condition")

_CONDITION_IMPORT_ERR = None
try:
    from fubon_neo.sdk import Condition, ConditionOrder, ConditionDayTrade
    from fubon_neo.constant import (BSAction, TimeInForce, TradingType,
                                    TriggerContent, Operator, StopSign,
                                    ConditionMarketType, ConditionPriceType,
                                    ConditionOrderType)
except ImportError as e:
    _CONDITION_IMPORT_ERR = str(e)


def _check_import():
    if _CONDITION_IMPORT_ERR:
        raise RuntimeError(f"SDK 條件單類別載入失敗（請確認 SDK ≥ 2.2.4）：{_CONDITION_IMPORT_ERR}")


def connect():
    import fubon_orders as fo
    sdk, acc = fo.connect_sdk(fo.fubon_cfg())
    try:
        sdk.init_realtime()
    except Exception:
        pass
    return sdk, acc


# ---------- 建構器 ----------
def _stop_condition_order(symbol, qty, trigger):
    _check_import()
    condition = Condition(
        market_type=TradingType.Reference,
        symbol=symbol,
        trigger=TriggerContent.MatchedPrice,
        trigger_value=str(trigger),
        comparison=Operator.GreaterThan)
    order = ConditionOrder(
        buy_sell=BSAction.Buy,
        symbol=symbol,
        price=str(trigger),
        quantity=int(qty),
        market_type=ConditionMarketType.Common,
        price_type=ConditionPriceType.Limit,
        time_in_force=TimeInForce.ROD,
        order_type=ConditionOrderType.Stock)
    return condition, order


def _entry_condition_order(symbol, qty, trigger, side, cmp_op):
    _check_import()
    condition = Condition(
        market_type=TradingType.Reference,
        symbol=symbol,
        trigger=TriggerContent.MatchedPrice,
        trigger_value=str(trigger),
        comparison=Operator.GreaterThan if cmp_op == "gt" else Operator.LessThan)
    order = ConditionOrder(
        buy_sell=BSAction.Sell if side == "sell" else BSAction.Buy,
        symbol=symbol,
        price=str(trigger),
        quantity=int(qty),
        market_type=ConditionMarketType.Common,
        price_type=ConditionPriceType.Limit,
        time_in_force=TimeInForce.ROD,
        order_type=ConditionOrderType.Stock)
    return condition, order


# ---------- 動作 ----------
def place_stop(sdk, acc, symbol, qty, trigger, log=None):
    """停損買回條件單：股價 > trigger → 現股限價買進（當日有效，未觸發自動失效）"""
    _check_import()
    from datetime import date as _d
    today = _d.today().strftime("%Y%m%d")
    condition, order = _stop_condition_order(symbol, qty, trigger)
    res = sdk.stock.single_condition(acc, today, today, StopSign.Full, condition, order)
    guid = (res.data.guid if getattr(res, "data", None) and hasattr(res.data, "guid")
            else None)
    ok = bool(res.is_success) if hasattr(res, "is_success") else True
    if log:
        log.info(f"停損條件單 {symbol} trigger={trigger} qty={qty} guid={guid} ok={ok}")
    return res, guid, ok


def place_entry_daytrade(sdk, acc, symbol, qty, trigger, side="sell", cmp_op="gt",
                         end_time="132000", log=None):
    """進場觸發當沖條件單：觸發 → 父單；13:20 前未反向平倉 → 自動市價回補"""
    _check_import()
    condition, order = _entry_condition_order(symbol, qty, trigger, side, cmp_op)
    daytrade_obj = ConditionDayTrade(
        day_trade_end_time="132000",   # 官方限制區間 130100~132000
        auto_cancel=True,
        price="",
        price_type=ConditionPriceType.Market)
    res = sdk.stock.single_condition_day_trade(
        acc, StopSign.Full, end_time, condition, order, daytrade_obj, None, True)
    guid = (res.data.guid if getattr(res, "data", None) and hasattr(res.data, "guid")
            else None)
    ok = bool(res.is_success) if hasattr(res, "is_success") else True
    if log:
        log.info(f"進場當沖條件單 {symbol} {side} trigger={trigger} qty={qty} "
                 f"guid={guid} ok={ok}（13:20 自動回補）")
    return res, guid, ok


def list_orders(sdk, acc):
    res = sdk.stock.get_condition_order(acc)
    return res


def cancel_order(sdk, acc, guid):
    res = sdk.stock.cancel_condition_orders(acc, guid)
    return res


# ---------- CLI ----------
def main():
    ap = argparse.ArgumentParser(description="富邦條件單（Smart Condition）工具")
    ap.add_argument("--list", action="store_true", help="查詢有效條件單")
    ap.add_argument("--cancel", metavar="GUID", help="取消條件單")
    ap.add_argument("--stop", metavar="SYMBOL", help="停損買回條件單（放空保護）")
    ap.add_argument("--entry", metavar="SYMBOL", help="進場觸發當沖條件單")
    ap.add_argument("--qty", type=int, default=1000)
    ap.add_argument("--trigger", help="觸發價")
    ap.add_argument("--side", choices=["buy", "sell"], default="sell", help="--entry 方向")
    ap.add_argument("--cmp", dest="cmp_op", choices=["gt", "lt"], default="gt",
                    help="比較：gt=觸發價以上（預設）、lt=以下")
    ap.add_argument("--end", default="132000", help="父單洗價結束時間（預設 132000）")
    a = ap.parse_args()
    log_ = log

    if not (a.list or a.cancel or a.stop or a.entry):
        ap.print_help()
        return

    try:
        sdk, acc = connect()
    except SystemExit as e:
        print(f"登入失敗：{e}")
        return

    try:
        if a.list:
            res = list_orders(sdk, acc)
            data = res.data if getattr(res, "data", None) else []
            rows = data if isinstance(data, list) else [data]
            print(f"有效條件單 {len(rows)} 筆：")
            for r in rows:
                def g(k, d="-"):
                    v = getattr(r, k, None) if not isinstance(r, dict) else r.get(k)
                    return v if v is not None else d
                print(f"  guid={g('guid')}  狀態={g('status')}  {g('condition_content', '')}")
                print(f"    {g('order_content', '')}")
        elif a.cancel:
            res = cancel_order(sdk, acc, a.cancel)
            print(f"取消結果：is_success={getattr(res, 'is_success', '?')} "
                  f"{getattr(res, 'message', '') or ''}")
        elif a.stop:
            res, guid, ok = place_stop(sdk, acc, a.stop, a.qty, a.trigger, log=log_)
            msg = getattr(res, "message", None)
            print(f"{'✓' if ok else '✗'} 停損買回條件單：{a.stop} 觸發>{a.trigger} qty={a.qty}｜"
                  f"guid={guid} ok={ok}" + (f"｜訊息：{msg}" if (not ok and msg) else ""))
            if not ok and msg and "效期" in str(msg):
                print("   （當日效期條件單限營業日 08:30–16:32 送出；預下單請用多條件單／停損停利單）")
        elif a.entry:
            res, guid, ok = place_entry_daytrade(sdk, acc, a.entry, a.qty, a.trigger,
                                                 side=a.side, cmp_op=a.cmp_op,
                                                 end_time=a.end, log=log_)
            msg = getattr(res, "message", None)
            print(f"{'✓' if ok else '✗'} 進場當沖條件單：{a.entry} {a.side} 觸發{a.cmp_op} "
                  f"{a.trigger} qty={a.qty}｜guid={guid} ok={ok}（13:20 自動回補）"
                  + (f"｜訊息：{msg}" if (not ok and msg) else ""))
    except Exception as e:
        print(f"✗ 條件單操作失敗：{e}")
        log.error(f"條件單操作失敗：{e}", exc_info=True)


if __name__ == "__main__":
    main()
