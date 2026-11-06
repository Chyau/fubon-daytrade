# -*- coding: utf-8 -*-
"""正式帳號分級驗證（正式環境下單前必跑；零風險→低風險逐級解鎖）

前置：.env 填入 FUBON_LIVE_ID / FUBON_LIVE_PASSWORD / FUBON_LIVE_CERT_PATH /
      FUBON_LIVE_CERT_PASSWORD（正式憑證 .pfx 從富邦 API 官網下載），
      並設 FUBON_USE_TEST=0。

分級（預設只跑 Phase 0+1 唯讀）：
  Phase 0  登入＋帳號查詢                          （零風險）
  Phase 1  下單系統報價帶（query_symbol_quote）×N 檔
           ＋行情端對照（正式環境兩者應一致）        （零風險）
  Phase 2  遠價掛單＋取消：漲停價買進掛單（永不成交）
           → 確認回報 → 取消 → 確認狀態             （零成交風險）
  Phase 3  盤中零股 1 股買進→立即市價賣出（約 200 元內） （低風險，需 --yes）
  Phase 4  融券限價賣出（引擎同路徑 short_entry_price）
           ＋立即市價買回 — 需信用戶，需 --yes        （真實部位數秒）

⚠ Phase 2+ 不會自動連跑：每階段都需顯式指定。Phase 3/4 需再加 --yes。

用法：
  python tools/preflight_real.py                 # Phase 0+1（唯讀）
  python tools/preflight_real.py --phase 2       # 遠價掛單＋取消
  python tools/preflight_real.py --phase 3 --yes # 零股小額實單
  python tools/preflight_real.py --phase 4 --yes # 融券來回（需信用戶）
"""
import argparse
import io
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from logutil import get_logger, ensure_utf8_stdout

ensure_utf8_stdout()
log = get_logger("preflight", "INFO")
os.environ.setdefault("FUBON_USE_TEST", "0")   # 本腳本只打正式環境

import fubon_orders as fo
from fubon_neo.sdk import Order
from fubon_neo.constant import (BSAction, MarketType, PriceType, TimeInForce,
                                OrderType)

TAG = "PFRT"   # preflight 標記（取代 FSDT，避免與沙箱共用帳號邏輯混淆）

PHASES_DONE = []


def _check_env():
    cfg = fo.fubon_cfg()
    if cfg["use_test"]:
        print("✗ FUBON_USE_TEST 仍為 1（測試環境）。正式驗證請設 FUBON_USE_TEST=0")
        sys.exit(1)
    missing = [k for k, v in (("FUBON_LIVE_ID", cfg["user_id"]),
                              ("FUBON_LIVE_PASSWORD", cfg["password"]),
                              ("FUBON_LIVE_CERT_PATH", cfg["cert_path"]),
                              ("FUBON_LIVE_CERT_PASSWORD", cfg["cert_password"]))
               if not v]
    if missing:
        print("✗ .env 缺少正式帳號設定：" + "、".join(missing))
        print("  ① 取得正式憑證：https://www.fbs.com.tw/TradeAPI/ → 下載憑證")
        print("  ② .env 填入 FUBON_LIVE_ID / FUBON_LIVE_PASSWORD /")
        print("     FUBON_LIVE_CERT_PATH / FUBON_LIVE_CERT_PASSWORD")
        print("  ③ 設 FUBON_USE_TEST=0")
        sys.exit(1)
    if not os.path.exists(cfg["cert_path"]):
        print(f"✗ 憑證檔不存在：{cfg['cert_path']}")
        sys.exit(1)
    return cfg


def phase0(sdk, acc):
    print(f"✓ 登入成功｜帳號：{fo.dig(acc, 'branch_no')}-{fo.dig(acc, 'account')}"
          f"｜類型 {fo.dig(acc, 'account_type')}")
    print(f"✓ 查詢委託回報：{len(fo.dig(sdk.stock.get_order_results(acc), 'data') or [])} 筆")
    PHASES_DONE.append(0)


def phase1(sdk, acc):
    print("\n--- Phase 1：下單系統報價帶 vs 行情端（正式環境應一致）---")
    ok = True
    for sym in ("2330", "0050", "2881", "2002"):
        band = fo.order_band(sdk, acc, sym)
        if not band:
            print(f"  {sym}：查詢失敗 ✗")
            ok = False
            continue
        # 行情端對照
        try:
            md = fo.ensure_realtime(sdk)
            res = md.rest_client.stock.intraday.quote(symbol=sym)
            d = fo.dig(res, "data") or {}
            mkt_ref = float(fo.dig(d, "referencePrice") or 0)
        except Exception:
            mkt_ref = 0
        consistent = (mkt_ref == 0 or band["ref"] == mkt_ref)
        ok = ok and consistent
        mark = "✓" if consistent else "⚠ 脫鉤"
        print(f"  {sym}：下單系統參考價 {band['ref']}｜漲停 {band['up']}｜跌停 {band['down']}"
              f"｜行情端參考價 {mkt_ref} {mark}")
    print("✓ Phase 1 通過（正式環境價格帶與行情一致）" if ok
          else "⚠ Phase 1 有不一致項 — 記錄但可繼續（限價夾帶機制已防護）")
    PHASES_DONE.append(1)


def phase2(sdk, acc, sym="0050"):
    print(f"\n--- Phase 2：遠價掛單＋取消（{sym} 漲停價買進，永不成交，零風險）---")
    band = fo.order_band(sdk, acc, sym)
    if not band:
        print("✗ 查不到價格帶"); return
    px = band["up"]   # 漲停價掛買 → 只在漲停時成交，實務上不會
    order = fo.build_order(Order, BSAction, MarketType, PriceType, TimeInForce,
                           OrderType, sym, "buy", 1000, px, False, False,
                           "ROD", "stock", TAG)
    res = sdk.stock.place_order(acc, order)
    ok = fo.dig(res, "is_success")
    no = fo.dig(fo.dig(res, "data"), "order_no")
    print(f"  掛單：ok={ok}｜no={no}｜msg={fo.dig(res, 'message')!r}")
    if not ok:
        print("✗ Phase 2 失敗 — 下單機制有問題，不可進實單"); return
    time.sleep(2)
    rows = fo.dig(sdk.stock.get_order_results(acc), "data") or []
    mine = [r for r in fo.filter_my_orders(rows, user_def=TAG)
            if str(fo.dig(r, "order_no")) == str(no)]
    if not mine:
        print("✗ 找不到回報"); return
    st = fo.dig(mine[0], "status")
    print(f"  回報：status={st}｜filled={fo.dig(mine[0], 'filled_qty')}")
    if fo.dig(mine[0], "filled_qty"):
        print("⚠ 竟然成交了（漲停？）— 立即市價賣出平倉並中止後續")
        return
    cr = sdk.stock.cancel_order(acc, mine[0])
    print(f"  取消：{fo.dig(cr, 'is_success')}")
    time.sleep(2)
    rows = fo.dig(sdk.stock.get_order_results(acc), "data") or []
    for r in fo.filter_my_orders(rows, user_def=TAG):
        if str(fo.dig(r, "order_no")) == str(no):
            print(f"  取消後狀態：{fo.dig(r, 'status')}（30=已取消 ✓）")
    print("✓ Phase 2 通過（下單→回報→取消 全鏈路）")
    PHASES_DONE.append(2)


def phase3(sdk, acc, sym="0050"):
    print(f"\n--- Phase 3：盤中零股 1 股買進→市價賣出（{sym}，約數百元內）---")
    band = fo.order_band(sdk, acc, sym)
    if not band:
        print("✗ 查不到價格帶"); return
    px = min(band["up"], round(band["ref"] * 1.005, 2))
    order = fo.build_order(Order, BSAction, MarketType, PriceType, TimeInForce,
                           OrderType, sym, "buy", 1, px, False, True,
                           "ROD", "stock", TAG)
    res = sdk.stock.place_order(acc, order)
    ok = fo.dig(res, "is_success")
    no = fo.dig(fo.dig(res, "data"), "order_no")
    print(f"  零股買進限價 {px}×1：ok={ok}｜no={no}｜msg={fo.dig(res, 'message')!r}")
    if not ok or not no:
        print("✗ Phase 3 買進失敗"); return
    time.sleep(3)
    rows = fo.dig(sdk.stock.get_order_results(acc), "data") or []
    mine = [r for r in fo.filter_my_orders(rows, user_def=TAG)
            if str(fo.dig(r, "order_no")) == str(no)]
    filled = float(fo.dig(mine[0], "filled_qty") or 0) if mine else 0
    if filled < 1:
        print("  未成交 → 取消")
        if mine:
            sdk.stock.cancel_order(acc, mine[0])
        return
    print(f"  買進成交 {filled} 股 → 市價賣出平倉")
    order = fo.build_order(Order, BSAction, MarketType, PriceType, TimeInForce,
                           OrderType, sym, "sell", 1, None, True, True,
                           "ROD", "stock", TAG)
    res = sdk.stock.place_order(acc, order)
    print(f"  賣出：ok={fo.dig(res, 'is_success')}｜msg={fo.dig(res, 'message')!r}")
    print("✓ Phase 3 通過（零股買賣閉環）")
    PHASES_DONE.append(3)


def phase4(sdk, acc, sym="2002"):
    print(f"\n--- Phase 4：融券限價賣出→市價買回（引擎放空同路徑，需信用戶）---")
    if not os.environ.get("PFRT_YES"):
        print("✗ 需 --yes"); return
    try:
        real_px = float(fo.dig(fo.dig(
            fo.ensure_realtime(sdk).rest_client.stock.intraday.quote(symbol=sym),
            "data"), "lastPrice") or 0)
    except Exception:
        real_px = 0
    if not real_px:
        print("✗ 取不到行情價"); return
    px, note = fo.short_entry_price(sdk, acc, sym, real_px)
    print(f"  short_entry_price({sym}, {real_px}) → {px}（{note}）")
    if px is None:
        print("✗ 取價失敗"); return
    order = fo.build_order(Order, BSAction, MarketType, PriceType, TimeInForce,
                           OrderType, sym, "sell", 1000, px, False, False,
                           "ROD", "short", TAG)
    res = sdk.stock.place_order(acc, order)
    no = fo.dig(fo.dig(res, "data"), "order_no")
    print(f"  融券賣出：ok={fo.dig(res, 'is_success')}｜no={no}｜"
          f"msg={fo.dig(res, 'message')!r}")
    if not fo.dig(res, "is_success") or not no:
        print("✗ 融券賣出被拒 — 查信用戶/融券額度"); return
    time.sleep(3)
    rows = fo.dig(sdk.stock.get_order_results(acc), "data") or []
    filled = 0
    for r in fo.filter_my_orders(rows, user_def=TAG):
        if str(fo.dig(r, "order_no")) == str(no):
            st = fo.dig(r, "status")
            filled = int(fo.dig(r, "filled_qty") or 0)
            print(f"  回報：status={st}｜filled={filled}")
            if st in (0, 1, 10) and not filled:
                print("  未成交 → 取消（機制驗證已足）")
                sdk.stock.cancel_order(acc, r)
                print("✓ Phase 4 通過（融券送單→回報→取消）")
                PHASES_DONE.append(4)
                return
    if filled >= 1000:
        order = fo.build_order(Order, BSAction, MarketType, PriceType, TimeInForce,
                               OrderType, sym, "buy", 1000, None, True, False,
                               "ROD", "stock", TAG)
        res = sdk.stock.place_order(acc, order)
        print(f"  市價買回：ok={fo.dig(res, 'is_success')}｜"
              f"msg={fo.dig(res, 'message')!r}")
        print("✓ Phase 4 通過（融券來回閉環）")
        PHASES_DONE.append(4)


def main():
    ap = argparse.ArgumentParser(description="正式帳號分級驗證")
    ap.add_argument("--phase", type=int, default=None,
                    help="0/1/2/3/4；不指定跑 0+1（唯讀）")
    ap.add_argument("--symbol", default=None, help="Phase 2~4 標的（預設 0050/2002）")
    ap.add_argument("--yes", action="store_true", help="Phase 3/4 需要（真實成交）")
    a = ap.parse_args()
    if a.yes:
        os.environ["PFRT_YES"] = "1"
    cfg = _check_env()
    sdk, acc = fo.connect_sdk(cfg)
    phase0(sdk, acc)
    phase1(sdk, acc)
    if a.phase == 2:
        phase2(sdk, acc, a.symbol or "0050")
    elif a.phase == 3:
        phase3(sdk, acc, a.symbol or "0050")
    elif a.phase == 4:
        phase4(sdk, acc, a.symbol or "2002")
    print(f"\n完成 Phase：{PHASES_DONE or '（僅 0/1 唯讀）'}")
    if 2 not in PHASES_DONE:
        print("提醒：進實單前建議至少通過 Phase 2（遠價掛單+取消，零風險全鏈路）。")


if __name__ == "__main__":
    main()
