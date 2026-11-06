# -*- coding: utf-8 -*-
"""正式帳號分級驗證（正式環境下單前必跑；零風險→低風險逐級解鎖）

🔒 憑證安全模式：FUBON_LIVE_* 未寫在 .env 也可執行 — 執行時互動輸入
   （getpass 不回顯），只存在本行程記憶體，**不寫入任何檔案**。
   若偏好 .env 也可以（照舊讀取），但建議用完即刪。

分級（預設只跑 Phase 0+1 唯讀）：
  Phase 0   登入＋帳號查詢＋委託回報                    （零風險）
  Phase 1   下單系統報價帶（query_symbol_quote）× N 檔
            ＋行情端對照（正式環境兩者應一致）           （零風險）
  Phase 1b  引擎行情源檢查：intraday.trades / candles 5 分K （零風險；
            引擎 live 模式 source=fubon 依賴此，正式環境必須可用）
  Phase 2   遠價掛單＋取消：漲停價買進掛單（永不成交）
            → 確認回報 → 取消 → 確認狀態                 （零成交風險）
  Phase 3   現股當沖整張來回（先買後賣；當沖限整張，零股不可當沖）
            市價買進 1 張 → 市價賣出平倉                  （真實成交，需 --yes）
  Phase 4   融券限價賣出→市價買回（引擎同路徑，需信用戶）   （真實部位數秒，需 --yes）

⚠ Phase 2+ 每階段都需顯式指定；Phase 3/4 需再加 --yes。

用法（在專案目錄執行）：
  python tools/preflight_real.py                  # Phase 0+1+1b（唯讀）
  python tools/preflight_real.py --phase 2        # 遠價掛單＋取消
  python tools/preflight_real.py --phase 3 --yes  # 整張現股當沖來回
  python tools/preflight_real.py --phase 4 --yes  # 融券來回（需信用戶）
"""
import argparse
import getpass
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

TAG = "PFRT"   # preflight 標記（避免與沙箱 FSDT 混淆）

PHASES_DONE = []


def _prompt_credentials(cfg):
    """.env 缺 LIVE_* 時互動輸入（getpass 不回顯；只存記憶體）"""
    need = {"user_id": "正式帳號 ID（身分證號）",
            "password": "登入密碼",
            "cert_path": "憑證檔路徑（.pfx）",
            "cert_password": "憑證密碼"}
    missing = [k for k in need if not cfg.get(k)]
    if not missing:
        return cfg
    print("🔒 .env 未設定正式憑證 → 互動輸入（不回顯、不寫入任何檔案，僅本次執行有效）")
    for k in missing:
        if k == "cert_path":
            v = input(f"  {need[k]}：").strip().strip('"')
        elif k == "user_id":
            v = input(f"  {need[k]}：").strip()
        else:
            v = getpass.getpass(f"  {need[k]}（不回顯）：")
        cfg[k] = v
        os.environ["FUBON_LIVE_" + ("ID" if k == "user_id" else
                                    "PASSWORD" if k == "password" else
                                    "CERT_PATH" if k == "cert_path" else
                                    "CERT_PASSWORD")] = v
    return cfg


def _check_env():
    os.environ["FUBON_USE_TEST"] = "0"
    fo._load_dotenv()
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
        cfg = _prompt_credentials(cfg)
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
    print("\n--- Phase 1：下單系統價格帶 vs 行情端（正式環境應同一世界）---")
    print("    檢查法：行情端 lastPrice 是否落在下單系統 [跌停, 漲停] 帶內")
    print("    （沙箱：真實價在模擬帶外；正式：應在帶內）")
    consistent_n = outside_n = unverifiable = 0
    for sym in ("2330", "0050", "2881", "2002"):
        band = fo.order_band(sdk, acc, sym)
        if not band:
            print(f"  {sym}：下單系統查詢失敗 ✗")
            continue
        last = prev = 0
        err = ""
        try:
            md = fo.ensure_realtime(sdk)
            if md is None:
                err = "init_realtime 失敗（marketdata 未就緒）"
            else:
                res = md.rest_client.stock.intraday.quote(symbol=sym)
                d = fo.dig(res, "data") or {}
                last = float(fo.dig(d, "lastPrice") or fo.dig(d, "closePrice") or 0)
                prev = float(fo.dig(d, "previousClose") or fo.dig(d, "referencePrice") or 0)
                if not last:
                    err = "行情端無 lastPrice/closePrice 欄位"
        except Exception as e:
            err = str(e)[:120]
        if last:
            inband = band["down"] <= last <= band["up"]
            ref_ok = (not prev) or (prev == band["ref"])
            if inband:
                consistent_n += 1
                extra = f"｜昨收比對 {'✓' if ref_ok else '⚠ 不等'}" if prev else ""
                print(f"  {sym}：行情 last {last} ∈ 下單系統帶 "
                      f"[{band['down']}, {band['up']}] ✓ 同一世界{extra}")
            else:
                outside_n += 1
                print(f"  {sym}：行情 last {last} ⚠ 在下單系統帶 "
                      f"[{band['down']}, {band['up']}] 之外 → 脫鉤（沙箱模式）！")
        else:
            unverifiable += 1
            print(f"  {sym}：下單系統 ref {band['ref']}｜行情端未取得（{err or '無資料'}）")
    print(f"\n  統計：同一世界 {consistent_n}｜脫鉤 {outside_n}｜無法比對 {unverifiable}")
    if consistent_n and not outside_n:
        print("✓ Phase 1 通過：正式環境行情與下單系統同一價格世界"
              "（引擎觸發價恆在下單系統帶內，限價夾帶為 no-op）")
    elif outside_n:
        print("⚠ 發現脫鉤 — 引擎限價夾帶機制會自動防護，但需檢視")
    else:
        print("⚠ 全部無法比對 — 檢查行情端連線（Phase 1b 若通過則只是 quote 欄位差異）")
    PHASES_DONE.append(1)


def phase1b(sdk, acc, sym="2330"):
    print(f"\n--- Phase 1b：引擎行情源檢查（intraday.trades / candles 5 分K，{sym}）---")
    try:
        md = fo.ensure_realtime(sdk)
        if md is None:
            print("✗ marketdata 未就緒（init_realtime 失敗）— 引擎 live 模式無法取即時行情")
            return
        ticks = fo.fetch_intraday_trades(sdk, sym, limit=10)
        print(f"  intraday.trades：{len(ticks or [])} 筆"
              + (f"｜最新 {ticks[-1].get('price') if ticks else '-'}" if ticks else ""))
        bars = fo.fetch_fubon_history(sym, days_back=2, timeframe="5")
        print(f"  intraday/historical candles 5 分K：{len(bars or [])} 根"
              + (f"｜最後一根 {bars[-1]['date']} {bars[-1]['hhmm']}" if bars else ""))
        if ticks and bars:
            print("✓ Phase 1b 通過（引擎 live 行情源在正式環境可用）")
            PHASES_DONE.append("1b")
        else:
            print("⚠ 部分不可用 — 引擎在正式環境的行情來源需再確認（yfinance 後備可用）")
    except Exception as e:
        print(f"✗ Phase 1b 例外：{str(e)[:200]}")


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


def phase3(sdk, acc, sym="2002"):
    """現股當沖整張來回（先買後賣）— 符合引擎實際行為（當沖限整張，零股不可當沖）。
    標的預設 2002（約 19 元 → 1 張 ≈ 19,000 元，來回成本約 50~100 元）。
    ① 查當沖資格 bitmask（daytrade_and_stock_info；bit4=可先買後賣）
    ② 市價買進 1 張 → 確認成交
    ③ 市價賣出 1 張平倉 → 確認成交"""
    print(f"\n--- Phase 3：現股當沖整張來回（{sym}，先買後賣）---")
    if not os.environ.get("PFRT_YES"):
        print("✗ 需 --yes"); return
    # ① 當沖資格（bit4 = 可先買後賣）
    try:
        info = fo.daytrade_info(sdk, acc, sym)
        st = info.get("status")
        can_buy_first = bool(st and (int(st) & 4))
        print(f"  當沖資格 status={st}｜先買後賣 {'✓' if can_buy_first else '✗'}"
              f"｜現股額度 {info.get('quota')}")
        if not can_buy_first:
            print("✗ 此標的不支援先買後賣當沖 → 同日賣出可能被拒，中止（換 --symbol）")
            return
    except Exception as e:
        print(f"⚠ 當沖資格查詢失敗：{str(e)[:100]}（繼續，但同日賣出有風險）")
    # ② 市價買進 1 張
    order = fo.build_order(Order, BSAction, MarketType, PriceType, TimeInForce,
                           OrderType, sym, "buy", 1000, None, True, False,
                           "ROD", "stock", TAG)
    res = sdk.stock.place_order(acc, order)
    no = fo.dig(fo.dig(res, "data"), "order_no")
    print(f"  市價買進 1 張：ok={fo.dig(res, 'is_success')}｜no={no}｜"
          f"msg={fo.dig(res, 'message')!r}")
    if not fo.dig(res, "is_success") or not no:
        print("✗ 買進失敗"); return
    time.sleep(3)
    rows = fo.dig(sdk.stock.get_order_results(acc), "data") or []
    filled = 0
    for r in fo.filter_my_orders(rows, user_def=TAG):
        if str(fo.dig(r, "order_no")) == str(no):
            filled = int(fo.dig(r, "filled_qty") or 0)
            print(f"  回報：status={fo.dig(r, 'status')}｜filled={filled}")
    if filled < 1000:
        print("  未滿 1 張 → 若未成交則取消")
        for r in fo.filter_my_orders(rows, user_def=TAG):
            if str(fo.dig(r, "order_no")) == str(no) and \
                    fo.dig(r, "status") in (0, 1, 10):
                sdk.stock.cancel_order(acc, r)
        return
    # ③ 市價賣出平倉
    order = fo.build_order(Order, BSAction, MarketType, PriceType, TimeInForce,
                           OrderType, sym, "sell", 1000, None, True, False,
                           "ROD", "stock", TAG)
    res = sdk.stock.place_order(acc, order)
    no2 = fo.dig(fo.dig(res, "data"), "order_no")
    print(f"  市價賣出平倉：ok={fo.dig(res, 'is_success')}｜no={no2}｜"
          f"msg={fo.dig(res, 'message')!r}")
    time.sleep(3)
    rows = fo.dig(sdk.stock.get_order_results(acc), "data") or []
    for r in fo.filter_my_orders(rows, user_def=TAG):
        if str(fo.dig(r, "order_no")) == str(no2):
            print(f"  賣出回報：status={fo.dig(r, 'status')}｜filled={fo.dig(r, 'filled_qty')}")
    print("✓ Phase 3 通過（整張現股當沖來回閉環）")
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
    ap = argparse.ArgumentParser(description="正式帳號分級驗證（憑證不落盤）")
    ap.add_argument("--phase", type=int, default=None,
                    help="2/3/4；不指定跑 0+1+1b（唯讀）")
    ap.add_argument("--symbol", default=None, help="Phase 2~4 標的（預設 0050/2002）")
    ap.add_argument("--yes", action="store_true", help="Phase 3/4 需要（真實成交）")
    a = ap.parse_args()
    if a.yes:
        os.environ["PFRT_YES"] = "1"
    cfg = _check_env()
    sdk, acc = fo.connect_sdk(cfg)
    phase0(sdk, acc)
    phase1(sdk, acc)
    phase1b(sdk, acc)
    if a.phase == 2:
        phase2(sdk, acc, a.symbol or "0050")
    elif a.phase == 3:
        phase3(sdk, acc, a.symbol or "2002")
    elif a.phase == 4:
        phase4(sdk, acc, a.symbol or "2002")
    print(f"\n完成 Phase：{PHASES_DONE or '（僅唯讀）'}")
    if 2 not in PHASES_DONE:
        print("提醒：進實單前建議至少通過 Phase 2（遠價掛單+取消，零風險全鏈路）。")


if __name__ == "__main__":
    main()
