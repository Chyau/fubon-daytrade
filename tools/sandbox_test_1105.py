# -*- coding: utf-8 -*-
"""沙箱三項關鍵測試（09:30 後執行）
① 現股賣出（賣方從未測過；庫存 2881 現貨 500 張）
② 限價融券賣出（決定引擎做空能否在沙箱跑；昨市價融券被拒「停止融券賣出」）
③ 市價停損條件單（今日新改的 Market 版，驗證 price="" 寫法）
＋ 清理所有 FSDT 委託/條件單
"""
import io
import sys
sys.path.insert(0, r"C:\Users\Chyau\Documents\預設專案\fubon-daytrade")
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import time
import fubon_orders as fo
import fubon_condition as fc
from fubon_neo.sdk import Order
from fubon_neo.constant import (BSAction, MarketType, PriceType, TimeInForce,
                                OrderType)

fo._load_dotenv()
assert fo.fubon_cfg()["use_test"], "必須是測試沙箱！"
sdk, acc = fo.connect_sdk(fo.fubon_cfg())
print(f"✓ 沙箱帳號：{fo.dig(acc, 'branch_no')}-{fo.dig(acc, 'account')}\n")

# ---------- ① 現股賣出（庫存） ----------
print("=== ① 現股賣出 2881 × 1張（market ROD，賣方首測） ===")
try:
    order = fo.build_order(Order, BSAction, MarketType, PriceType, TimeInForce,
                           OrderType, "2881", "sell", 1000, None, True, False,
                           "ROD", "stock", "FSDT")
    res = sdk.stock.place_order(acc, order)
    data = fo.dig(res, "data")
    print(f"  is_success={fo.dig(res, 'is_success')}｜message={fo.dig(res, 'message')!r}"
          f"｜order_no={fo.dig(data, 'order_no') if data else None}")
except Exception as e:
    print(f"  例外：{str(e)[:200]}")

# ---------- ② 限價融券賣出 ----------
print("\n=== ② 限價融券賣出（Short + Limit）===")
for sym, px in (("2317", 245.0), ("2002", 45.0)):
    try:
        order = fo.build_order(Order, BSAction, MarketType, PriceType, TimeInForce,
                               OrderType, sym, "sell", 1000, px, False, False,
                               "ROD", "short", "FSDT")
        res = sdk.stock.place_order(acc, order)
        data = fo.dig(res, "data")
        print(f"  {sym} 限價{px}｜is_success={fo.dig(res, 'is_success')}"
              f"｜message={fo.dig(res, 'message')!r}"
              f"｜order_no={fo.dig(data, 'order_no') if data else None}")
    except Exception as e:
        print(f"  {sym} 例外：{str(e)[:200]}")

# ---------- ③ 市價停損條件單 ----------
print("\n=== ③ 市價停損條件單（Market 版） ===")
try:
    res, guid, ok = fc.place_stop(sdk, acc, "2330", 1000, 2300.0)
    print(f"  is_success={ok}｜guid={guid}｜message={fo.dig(res, 'message')!r}")
except Exception as e:
    print(f"  例外：{str(e)[:200]}")

# ---------- 清理 ----------
print("\n=== 清理：取消所有 FSDT 條件單與未成交委託 ===")
try:
    for r in fc.list_orders(sdk, acc) or []:
        guid = fo.dig(r, "guid")
        if guid:
            try:
                cr = fc.cancel_order(sdk, acc, guid)
                print(f"  條件單 {guid} 取消 → {fo.dig(cr, 'is_success')}")
            except Exception as e:
                print(f"  條件單 {guid} 取消失敗：{str(e)[:80]}")
except Exception as e:
    print(f"  條件單列表失敗：{str(e)[:100]}")

time.sleep(2)
try:
    rows = fo.dig(sdk.stock.get_order_results(acc), "data") or []
    mine = fo.filter_my_orders(rows, user_def="FSDT")
    for r in mine:
        ono = fo.dig(r, "order_no")
        st = fo.dig(r, "status")
        print(f"  委託 {ono}｜{fo.dig(r, 'stock_no')}｜{fo.dig(r, 'buy_sell')}"
              f"｜{fo.dig(r, 'order_type')}｜status={st}｜filled={fo.dig(r, 'filled_qty')}")
        if st in (10, 1) and ono:
            try:
                cr = sdk.stock.cancel_order(acc, r)
                print(f"    → 已取消（{fo.dig(cr, 'is_success')}）")
            except Exception as e:
                print(f"    → 取消失敗：{str(e)[:80]}")
except Exception as e:
    print(f"  清理失敗：{str(e)[:100]}")

print("\n=== 測試完成 ===")
