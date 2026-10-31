# -*- coding: utf-8 -*-
"""
富邦新一代 API 下單工具 — PLACE_ORDER 安全開關
=================================================
規格（依使用者要求）：
  PLACE_ORDER = false（預設）
    → 沒有真實下單。只輸出「富邦新一代API下單參數」（Order 物件欄位 + 等效程式碼），
      有 SDK＋憑證時附帶漲跌停／當沖資格驗證；未安裝 SDK 也能輸出參數。
  PLACE_ORDER = true
    → 真實下單。仍需加 --send（雙重開關，避免誤送）：
      - FUBON_USE_TEST=1（測試沙箱，零風險、庫存每日重設）：--yes 即可送
      - FUBON_USE_TEST=0（正式盤，實單）：--yes 且互動輸入股票代號二次確認
        ※ 真實盤需設定 FUBON_USE_TEST=0 並填妥：
          FUBON_LIVE_ID / FUBON_LIVE_PASSWORD / FUBON_LIVE_CERT_PATH / FUBON_LIVE_CERT_PASSWORD

用法：
  python fubon_orders.py --dry   --symbol 2344 --side buy  --qty 1000 --price 60
  python fubon_orders.py --dry   --symbol 2344 --side sell --qty 1000 --short --price 60   # 融券放空
  python fubon_orders.py --dry   --symbol 2344 --side sell --qty 1000 --market                # 市價
  python fubon_orders.py --send  --symbol 2344 --side buy  --qty 1000 --price 60 --yes        # PLACE_ORDER=true 才會送
  python fubon_orders.py --status    # 查今日委託與成交
  python fubon_orders.py --cancel ORDER_NO

其他環境變數：FUBON_USE_TEST / FUBON_TEST_* / FUBON_LIVE_*（見 .env.example）
"""
import json
import os
import sys
import time

from logutil import get_logger, ensure_utf8_stdout

ensure_utf8_stdout()

log = get_logger("orders")   # logging：logs/fubon_daytrade.log（DEBUG 全量）＋主控台

BANNER = "=" * 64


def _load_dotenv():
    """讀取專案目錄 .env（已存在的環境變數不被覆蓋）"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    try:
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and v and k not in os.environ:
                os.environ[k] = v
    except Exception as e:
        log.debug(f".env 載入略過：{e}")


_load_dotenv()


def env_bool(k, d=False):
    v = os.environ.get(k, "")
    return d if v == "" else v.strip().lower() in ("true", "1", "yes", "y", "on")


def _resolve_cert(p):
    """憑證路徑自動解析：相對路徑以專案目錄為基準，並嘗試 test_environment 子目錄"""
    if not p:
        return p
    if os.path.isabs(p):
        return p
    base = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(base, p),
                 os.path.join(base, os.path.basename(p)),
                 os.path.join(base, "test_environment", os.path.basename(p))):
        if os.path.exists(cand):
            return cand
    return p


def fubon_cfg():
    """依 FUBON_USE_TEST 選擇測試／正式環境設定"""
    use_test = os.environ.get("FUBON_USE_TEST", "1").strip().lower() not in ("0", "false", "no", "off")
    if use_test:
        return {"use_test": True,
                "url": os.environ.get("FUBON_TEST_URL") or "wss://neoapitest.fbs.com.tw/TASP/XCPXWS",
                "user_id": os.environ.get("FUBON_TEST_ID", ""),
                "password": os.environ.get("FUBON_TEST_PASSWORD", ""),
                "cert_path": _resolve_cert(os.environ.get("FUBON_TEST_CERT_PATH", "")),
                "cert_password": os.environ.get("FUBON_TEST_CERT_PASSWORD", "")}
    return {"use_test": False, "url": None,
            "user_id": os.environ.get("FUBON_LIVE_ID", ""),
            "password": os.environ.get("FUBON_LIVE_PASSWORD", ""),
            "cert_path": _resolve_cert(os.environ.get("FUBON_LIVE_CERT_PATH", "")),
            "cert_password": os.environ.get("FUBON_LIVE_CERT_PASSWORD", "")}


def dig(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def has_sdk():
    try:
        from fubon_neo.sdk import FubonSDK, Order  # noqa
        from fubon_neo.constant import BSAction, MarketType, PriceType, TimeInForce, OrderType  # noqa
        return True
    except ImportError:
        return False


SETUP_HINT = """【SDK 或憑證未就緒】
1. SDK 未安裝：到 https://www.fbs.com.tw/TradeAPI/docs/download/download-sdk
   下載 .whl 後執行：pip install <檔名>.whl（SDK 不在 PyPI）
2. 測試憑證：下載 test_environment.zip 解壓 .pfx，設定 FUBON_TEST_* 環境變數
   （帳號=憑證 CN 值；登入密碼與憑證密碼皆為官方預設 12345678）
3. 注意：PLACE_ORDER=false 時本工具仍可輸出下單參數（不需 SDK、不會送單）"""


def connect_sdk(cfg, retries=3):
    from fubon_neo.sdk import FubonSDK
    env_name = "測試沙箱" if cfg["use_test"] else "正式（實單）"
    log.info(f"登入{env_name}：帳號={cfg['user_id'] or '(未設定)'} 憑證={cfg['cert_path'] or '(未設定)'}")
    log.debug(f"連線設定（不含密碼）：{ {k: v for k, v in cfg.items() if 'password' not in k} }")
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            if cfg["use_test"]:
                sdk = FubonSDK(30, 2, url=cfg["url"])   # SDK 2.2.1+：參考值 30/2；測試環境加 url
            else:
                sdk = FubonSDK(30, 2)
            if cfg["cert_password"]:
                acc = sdk.login(cfg["user_id"], cfg["password"], cfg["cert_path"], cfg["cert_password"])
            else:
                # SDK >= 1.3.2 預設憑證密碼：省略第 4 參數（不可傳空字串）
                acc = sdk.login(cfg["user_id"], cfg["password"], cfg["cert_path"])
            if acc is not None and getattr(acc, "is_success", True) is False:
                log.error(f"登入失敗：{getattr(acc, 'message', acc)}")
                raise SystemExit(f"富邦登入失敗：{getattr(acc, 'message', acc)}")
            log.info("登入成功")
            return sdk, acc
        except SystemExit:
            raise
        except Exception as e:
            last_err = e
            log.warning(f"連線異常（第 {attempt}/{retries} 次）：{str(e)[:100]}")
            if attempt < retries:
                time.sleep(2)
    raise RuntimeError(
        f"網路無法連上富邦主機（DNS／防火牆／VPN 問題，非憑證問題）— 已重試 {retries} 次｜"
        f"最後錯誤：{str(last_err)[:120]}")
    # 選證券帳戶（排除期貨/複委託）
    stock_accs = [a for a in (dig(acc, "data") or [])
                  if dig(a, "account_type") in (None, "stock", "")] or list(dig(acc, "data") or [])
    if not stock_accs:
        raise SystemExit("登入成功但找不到可用帳戶")
    return sdk, stock_accs[0]


def build_order(Order, BSAction, MarketType, PriceType, TimeInForce, OrderType,
                symbol, side, qty, price, market_flag, odd, tif, ot, user_def):
    """組出富邦新一代 Order 物件。數量一律為股數（1 張 = 1000 股）。"""
    tif_val = {"ROD": TimeInForce.ROD, "IOC": TimeInForce.IOC, "FOK": TimeInForce.FOK}.get(
        (tif or "ROD").upper(), TimeInForce.ROD)
    ot_val = {"stock": OrderType.Stock,
              "short": OrderType.ShortSelling,    # 融券賣出（先賣後買當沖）
              "margin": OrderType.MarginTrading}  # 融資買進
    is_market = market_flag or not price
    return Order(
        buy_sell=BSAction.Buy if side == "buy" else BSAction.Sell,
        symbol=symbol,
        price="" if is_market else str(price),    # 市價單 price 傳空字串
        quantity=int(qty),
        market_type=MarketType.IntradayOdd if odd else MarketType.Common,
        price_type=PriceType.Market if is_market else PriceType.Limit,
        time_in_force=tif_val,
        order_type=ot_val.get(ot, OrderType.Stock),
        user_def=(user_def or "ORB")[:10],
    )


def validate(rest, ticker, acc, symbol, odd, price):
    """漲跌停驗證（query_symbol_quote）+ 當沖資格（intraday.ticker.canDayTrade）"""
    from fubon_neo.constant import MarketType
    msgs, lu, ld, dt = [], None, None, None
    if acc is None:
        return ["（未登入：無法驗證漲跌停/當沖資格）"], lu, ld, dt
    try:
        if odd:
            q = dig(rest.query_symbol_quote(acc, symbol, MarketType.IntradayOdd), "data")
        else:
            q = dig(rest.query_symbol_quote(acc, symbol), "data")
        raw = dig(q, "raw") or q
        lu = dig(raw, "limitUpPrice") or dig(q, "limitUpPrice")
        ld = dig(raw, "limitDownPrice") or dig(q, "limitDownPrice")
        if lu and price:
            if float(price) > float(lu):
                msgs.append(f"✗ 委託價 {price} 高於漲停 {lu}")
            elif float(price) < float(ld):
                msgs.append(f"✗ 委託價 {price} 低於跌停 {ld}")
            else:
                msgs.append(f"✓ 委託價在漲跌停區間內（{ld} ~ {lu}）")
    except Exception as e:
        msgs.append(f"（漲跌停驗證失敗：{str(e)[:60]}）")
    try:
        t = dig(ticker.intraday.ticker(symbol=symbol), "data")
        dt = dig(t, "canDayTrade")
        if dt is False:
            msgs.append("⚠ 此股不可現股當沖（canDayTrade=False）")
    except Exception:
        pass
    return msgs, lu, ld, dt


def describe_order(side, symbol, price, qty, market_flag, odd, tif, ot, user_def):
    is_market = market_flag or not price
    ot_label = {"stock": "Stock(現股)",
                "short": "ShortSelling(融券賣出/先賣後買)",
                "margin": "MarginTrading(融資買進)"}[ot]
    return {
        "buy_sell": f"BSAction.{'Buy' if side == 'buy' else 'Sell'}",
        "symbol": symbol,
        "price": "（市價→空字串）" if is_market else str(price),
        "quantity": f"{qty} 股（{qty // 1000} 張 + {qty % 1000} 股）",
        "market_type": "IntradayOdd(盤中零股)" if odd else "Common(整股)",
        "price_type": "Market(市價)" if is_market else "Limit(限價)",
        "time_in_force": tif,
        "order_type": ot_label,
        "user_def": (user_def or "ORB")[:10],
    }


def do_dry(cfg, args):
    """PLACE_ORDER=false：沒有真實下單，只輸出富邦新一代API下單參數（＋可選驗證）"""
    qty = int(args.qty)
    validation = ["（未登入：無法驗證漲跌停/當沖資格）"]
    sdk = None
    if has_sdk():
        if cfg["user_id"] and cfg["password"] and cfg["cert_path"]:
            try:
                sdk, acc = connect_sdk(cfg)
                rest = sdk.marketdata.rest_client.stock
                validation, _, _, _ = validate(rest, rest, acc, args.symbol, args.odd, args.price)
            except SystemExit:
                raise
            except Exception as e:
                validation = [f"（登入/驗證失敗：{str(e)[:80]}）"]
        else:
            validation = ["（憑證未設定：無法登入驗證；仍輸出下單參數）"]
    else:
        validation = ["（SDK 未安裝：僅輸出下單參數；安裝後可加漲跌停/當沖驗證）"]
    if not args.odd and qty % 1000 != 0:
        validation.append(f"⚠ 整股數量 {qty} 非 1000 股整數倍（整股最小 1 張=1000 股；零股請加 --odd）")
    if args.odd and qty >= 1000:
        validation.append("⚠ 盤中零股單筆上限 999 股")

    is_market = args.market or not args.price
    price_repr = "''" if is_market else f"'{args.price}'"
    ot_name = {"stock": "OrderType.Stock", "short": "OrderType.ShortSelling",
               "margin": "OrderType.MarginTrading"}[args.ot]

    print(BANNER)
    print(f"【DRY-RUN｜下單參數輸出】PLACE_ORDER={os.environ.get('PLACE_ORDER', 'false')} → 沒有真實下單")
    print(f"環境：{'測試沙箱' if cfg['use_test'] else '正式（FUBON_USE_TEST=0）'} ｜ 帳號：{cfg['user_id'] or '(未設定)'}")
    print(BANNER)
    print("富邦新一代API下單參數（sdk.stock.place_order 的 Order 物件）：")
    for k, v in describe_order(args.side, args.symbol, args.price, qty,
                               args.market, args.odd, args.tif, args.ot, args.user_def).items():
        print(f"  {k:<14} = {v}")
    print("等效程式碼：")
    print(f"  order = Order(buy_sell=BSAction.{'Buy' if args.side == 'buy' else 'Sell'}, "
          f"symbol='{args.symbol}', price={price_repr}, quantity={qty},")
    print(f"              market_type=MarketType.{'IntradayOdd' if args.odd else 'Common'}, "
          f"price_type=PriceType.{'Market' if is_market else 'Limit'},")
    print(f"              time_in_force=TimeInForce.{args.tif}, order_type={ot_name},")
    print(f"              user_def='{(args.user_def or 'ORB')[:10]}')")
    print(f"  res = sdk.stock.place_order(account, order)")
    print(BANNER)
    for m in validation:
        print(" ", m)
        (log.warning if m.startswith(("⚠", "✗")) else log.debug)(m)
    print("→ 要真實下單：set PLACE_ORDER=true 後加 --send（"
          + ("測試沙箱：--yes 即送" if cfg["use_test"]
             else "正式盤：FUBON_USE_TEST=0 + FUBON_LIVE_* + --yes + 輸入代號確認") + "）")
    if sdk:
        try:
            sdk.logout()
        except Exception:
            pass


def do_send(cfg, args):
    """PLACE_ORDER=true：真實下單（須 --send 觸發；false 時自動轉 dry-run）"""
    if not env_bool("PLACE_ORDER", False):
        log.warning("--send 被拒：PLACE_ORDER 未開啟（預設 false）→ 沒有真實下單，轉為 dry-run。")
        print("PLACE_ORDER 未開啟（預設 false）→ 沒有真實下單，轉為 dry-run 參數輸出。")
        print("要真實下單：set PLACE_ORDER=true 後再執行 --send。")
        return do_dry(cfg, args)
    from fubon_neo.sdk import Order
    from fubon_neo.constant import BSAction, MarketType, PriceType, TimeInForce, OrderType
    try:
        from fubon_neo.fugle_marketdata.rest.base_rest import FugleAPIError
    except ImportError:
        try:
            from fubon_neo import FugleAPIError  # 較新 SDK 的匯出位置
        except ImportError:
            class FugleAPIError(Exception):
                pass  # 型別不可得時，交給下方 except Exception 接住

    sdk, acc = connect_sdk(cfg)
    rest = sdk.marketdata.rest_client.stock
    validation, _, _, _ = validate(rest, rest, acc, args.symbol, args.odd, args.price)
    qty = int(args.qty)
    if not args.odd and qty % 1000 != 0:
        print(f"✗ 整股數量必須為 1000 股整數倍（目前 {qty}）。零股請加 --odd。")
        sdk.logout()
        return
    bad = [m for m in validation if m.startswith("✗")]
    if bad and not args.force:
        print("驗證未通過（--force 可強制）：")
        for m in bad:
            print("  ", m)
        sdk.logout()
        return

    # 最後一道確認閘
    print(BANNER)
    print(f"環境：{'測試沙箱' if cfg['use_test'] else '⚠ 正式環境（實單）'} ｜ "
          f"{args.side.upper()} {args.symbol} × {qty} 股 @ "
          f"{'市價' if (args.market or not args.price) else args.price} ｜ {args.ot}")
    for m in validation:
        print("  ", m)
    if cfg["use_test"]:
        confirm = args.yes
        if not confirm:
            print("測試環境需加 --yes 才會送單。")
    else:
        ans = input("正式環境：輸入股票代號以確認真實下單（其他輸入取消）：").strip().upper()
        confirm = (ans == args.symbol.upper())
    if not confirm:
        print("已取消，未送單。")
        sdk.logout()
        return

    order = build_order(Order, BSAction, MarketType, PriceType, TimeInForce, OrderType,
                        args.symbol, args.side, qty, args.price, args.market, args.odd,
                        args.tif, args.ot, args.user_def)

    fills = []

    def on_filled(d):
        fills.append(d)
        print(f"  [成交回報] {dig(d, 'symbol')} 均價 {dig(d, 'filled_avg_price')} "
              f"量 {dig(d, 'filled_qty')}", flush=True)
        log.info(f"[成交回報] {dig(d, 'symbol')} 均價 {dig(d, 'filled_avg_price')} "
                 f"量 {dig(d, 'filled_qty')}")

    try:
        sdk.set_on_filled(on_filled)
    except Exception:
        pass
    mode = "live" if not cfg["use_test"] else "test"
    order_payload = {
        "buy_sell": str(dig(order, "buy_sell")), "symbol": args.symbol, "quantity": qty,
        "price": str(dig(order, "price")), "market_type": str(dig(order, "market_type")),
        "price_type": str(dig(order, "price_type")), "time_in_force": str(dig(order, "time_in_force")),
        "order_type": str(dig(order, "order_type")), "user_def": dig(order, "user_def"),
    }
    log.info(f"[{mode}] 送出委託：{args.side} {args.symbol} × {qty} 股")
    log.debug(f"Order 參數：{order_payload}")

    def _record(order_no, ok, msg):
        """每次真實送單（成功/失敗）都寫入 sqlite orders 表，供日後學習複盤"""
        try:
            import db as tdb
            conn = tdb.connect()
            tdb.init_db(conn)
            tdb.record_order(conn, mode=mode, symbol=args.symbol, side=args.side,
                             order_type=args.ot, quantity=qty,
                             price=str(args.price or ""),
                             price_type="Market" if (args.market or not args.price) else "Limit",
                             market_type="IntradayOdd" if args.odd else "Common",
                             tif=args.tif, user_def=(args.user_def or "ORB")[:10],
                             order_no=str(order_no) if order_no else None,
                             is_success=int(bool(ok)), message=str(msg or "")[:200],
                             raw=json.dumps(order_payload, ensure_ascii=False, default=str))
            conn.close()
            log.debug("交易紀錄已寫入 sqlite orders 表")
        except Exception as dberr:
            log.error(f"交易紀錄寫入 DB 失敗：{dberr}")

    try:
        res = sdk.stock.place_order(acc, order)
        data = dig(res, "data")
        no = dig(data, "order_no") if data else None
        ok = bool(dig(res, "is_success", True))
        print(f"✓ 已送出真實委託：order_no = {no} ｜ is_success={ok}")
        log.info(f"委託送出 order_no={no} is_success={ok}")
        _record(no, ok, dig(res, "message"))
        try:
            import notify
            notify.send(f"🔔 富邦下單 [{mode}] {args.side} {args.symbol} × {qty} 股\n"
                        f"order_no={no}｜成功={ok}")
        except Exception:
            pass
    except FugleAPIError as e:
        msg = f"status={getattr(e, 'status_code', '?')} {str(getattr(e, 'response_text', e))[:150]}"
        print(f"✗ 送單失敗：{msg}")
        log.error(f"送單失敗：{msg}")
        _record(None, False, msg)
        try:
            import notify
            notify.send(f"⚠️ 富邦下單失敗 [{mode}] {args.symbol} {args.side}×{qty}：{msg[:150]}")
        except Exception:
            pass
        sdk.logout()
        return
    except Exception as e:
        print(f"✗ 送單失敗：{e}")
        log.error(f"送單失敗：{e}", exc_info=True)
        _record(None, False, str(e))
        try:
            import notify
            notify.send(f"⚠️ 富邦下單失敗 [{mode}] {args.symbol} {args.side}×{qty}：{str(e)[:150]}")
        except Exception:
            pass
        sdk.logout()
        return
    time.sleep(2)  # 等待回報
    try:
        results = sdk.stock.get_order_results(acc)
        data = dig(results, "data") or []
        print(f"今日委託/回報（{len(data)} 筆，最新 5 筆；status=30 代表已刪單）：")
        for r in (data[-5:] if isinstance(data, list) else []):
            print(f"  {dig(r, 'order_no')} {dig(r, 'symbol')} {dig(r, 'buy_sell')} "
                  f"{dig(r, 'quantity')}@{dig(r, 'price')} 狀態{dig(r, 'status')} 成交{dig(r, 'filled_qty')}")
        if fills:
            print(f"成交回報 {len(fills)} 筆（on_filled callback）")
    except Exception as e:
        print("查詢委託失敗：", e)
    sdk.logout()


def do_status(cfg):
    sdk, acc = connect_sdk(cfg)
    results = sdk.stock.get_order_results(acc)
    data = dig(results, "data") or []
    print(f"今日委託 {len(data)} 筆（status=30 代表已刪單）：")
    for r in data:
        print(f"  {dig(r, 'order_no')} {dig(r, 'symbol')} {dig(r, 'buy_sell')} "
              f"{dig(r, 'quantity')}@{dig(r, 'price')} 狀態{dig(r, 'status')} 成交{dig(r, 'filled_qty')}")
    sdk.logout()


def do_cancel(cfg, order_no):
    sdk, acc = connect_sdk(cfg)
    results = dig(sdk.stock.get_order_results(acc), "data") or []
    target = next((r for r in results if str(dig(r, "order_no")) == str(order_no)), None)
    if not target:
        print(f"找不到委託 {order_no}（status=30 已刪單也會列在回報中）")
        sdk.logout()
        return
    try:
        res = sdk.stock.cancel_order(acc, target)
        print(f"✓ 已送出刪單：{dig(res, 'is_success', True)} {dig(res, 'message') or ''}")
    except Exception as e:
        print(f"✗ 刪單失敗：{e}")
    sdk.logout()


def main():
    import argparse
    ap = argparse.ArgumentParser(description="富邦新一代 API 下單工具（PLACE_ORDER 安全開關）")
    ap.add_argument("--dry", action="store_true", help="只輸出下單參數，不送單（預設行為）")
    ap.add_argument("--send", action="store_true", help="真實下單請求：PLACE_ORDER=true 才送，否則轉 dry-run")
    ap.add_argument("--status", action="store_true", help="查今日委託與成交")
    ap.add_argument("--cancel", metavar="ORDER_NO")
    ap.add_argument("--symbol", required="--dry" in sys.argv or "--send" in sys.argv)
    ap.add_argument("--side", choices=["buy", "sell"], default="buy")
    ap.add_argument("--qty", type=int, required="--symbol" in sys.argv, help="股數（整股 1000 的倍數；盤中零股 1~999）")
    ap.add_argument("--price", help="限價（省略或加 --market = 市價）")
    ap.add_argument("--market", action="store_true", help="市價單（price 傳空字串）")
    ap.add_argument("--odd", action="store_true", help="盤中零股（1~999 股）")
    ap.add_argument("--short", dest="ot", action="store_const", const="short", default="stock",
                    help="融券賣出 OrderType.ShortSelling（先賣後買當沖；僅作用於 sell）")
    ap.add_argument("--margin", dest="ot", action="store_const", const="margin",
                    help="融資買進 OrderType.MarginTrading")
    ap.add_argument("--tif", default="ROD", choices=["ROD", "IOC", "FOK"])
    ap.add_argument("--user-def", dest="user_def", default="ORB", help="自訂註記 ≤10 字元")
    ap.add_argument("--yes", action="store_true", help="測試環境送單確認")
    ap.add_argument("--force", action="store_true", help="忽略驗證警告強制送出")
    a = ap.parse_args()

    if a.ot == "short" and a.side == "buy":
        print("註：--short 僅作用於 sell（融券賣出）；buy 一律以現股 OrderType.Stock 處理（可用於買回融券部位）。")
        a.ot = "stock"

    need_sdk = a.status or a.cancel or a.send
    if need_sdk and not has_sdk():
        print(SETUP_HINT)
        return
    cfg = fubon_cfg()
    if a.status:
        do_status(cfg)
    elif a.cancel:
        do_cancel(cfg, a.cancel)
    elif a.send:
        do_send(cfg, a)
    else:
        do_dry(cfg, a)


# ==================== 行情資料（供回測／自動執行共用） ====================
def _parse_candle_rows(rows):
    """富邦 intraday.candles 回傳 → strategy 相容 bars（時間格式自動判別）"""
    bars = []
    for r in rows or []:
        try:
            ts = str(r.get("date") or r.get("time") or "")
            if "T" in ts or " " in ts:
                date, hhmm = ts[:10], ts[11:16]
            elif len(ts) >= 12:
                date, hhmm = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}", f"{ts[8:10]}:{ts[10:12]}"
            else:
                continue
            bars.append({"date": date, "hhmm": hhmm, "time": f"{date} {hhmm}",
                         "open": round(float(r["open"]), 2),
                         "high": round(float(r["high"]), 2),
                         "low": round(float(r["low"]), 2),
                         "close": round(float(r["close"]), 2),
                         "volume": float(r.get("volume", 0) or 0)})
        except (KeyError, TypeError, ValueError):
            continue
    return bars


def fetch_intraday_bars(sdk, symbol, timeframe="5"):
    """最新交易日的 5 分K（富邦行情；需先 sdk.init_realtime()）"""
    res = sdk.marketdata.rest_client.stock.intraday.candles(symbol=symbol, timeframe=timeframe)
    return _parse_candle_rows(dig(res, "data") or dig(res, "raw") or [])


def fetch_fubon_history(symbol, days_back=60, timeframe="5", log=None):
    """真實歷史分K（historical/candles；分K 自 2023-05-23 起提供，單次區間 <1 年）。
    失敗時退回 intraday 最新交易日。"""
    from datetime import date as _d, timedelta as _td
    sdk, acc = connect_sdk(fubon_cfg())
    try:
        sdk.init_realtime()   # 行情連線（marketdata 需要）
    except Exception:
        pass
    bars = []
    to = _d.today()
    frm = to - _td(days=days_back)
    try:
        res = sdk.marketdata.rest_client.stock.historical.candles(**{
            "symbol": symbol, "from": frm.isoformat(), "to": to.isoformat(),
            "timeframe": timeframe, "sort": "asc",
            "fields": "open,high,low,close,volume,average"})
        rows = dig(res, "data") or dig(res, "raw") or []
        bars = _parse_candle_rows(rows)
        if log and bars:
            days_n = len(set(b["date"] for b in bars))
            log.info(f"富邦歷史分K {symbol}：{len(bars)} 根、{days_n} 個交易日"
                     f"（{bars[0]['date']} ~ {bars[-1]['date']}）")
    except Exception as e:
        if log:
            log.warning(f"歷史分K取得失敗，改用當日行情：{str(e)[:80]}")
    if not bars:
        try:
            bars = fetch_intraday_bars(sdk, symbol, timeframe)
        except Exception as e:
            if log:
                log.warning(f"intraday 亦失敗：{str(e)[:60]}")
    try:
        sdk.logout()
    except Exception:
        pass
    return bars


if __name__ == "__main__":
    main()
