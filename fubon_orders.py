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
import math
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


def filter_my_orders(rows, user_def="FSDT", order_nos=None):
    """共用測試帳號下過濾「自己的」委託回報。

    沙箱/正式帳號可能多人共用（get_order_results 回傳所有人的單），
    辨別依據（任一符合）：
      1. user_def == user_def 參數（下單時蓋的章，回報原封帶回）
      2. order_no 在 order_nos 白名單（place_order 回傳時自己記下來的）

    ⚠沙箱實測（2026-11-05 盤後）：融券（Short）預約單的 user_def 會被沙箱
    吃掉變 ' '、order_no=None、status=0 → 只能靠 order_nos 白名單辨識。
    盤中委託則 order_no 正常回傳。清理殘留單時不能用 user_def 過濾，
    要掃全部 Short+Sell 未完成單。

    ⚠沙箱下單矩陣（2026-11-06 盤中終局實測，2317/2002/2330/2881）：
      現股市價買/賣 → ✓（order_no 正常）
      限價單（現股/融券 × ROD/IOC × 任何合法價位）→ ✗ 4385715 單價輸入錯誤
      融券市價（放空進場）→ ✗ 4385034 停止融券賣出
      → 沙箱盤中「放空」完全不可實單（市價/限價皆拒）＝平台限制，客戶端無解；
         沙箱可實單驗證的只有「現股做多」腿（市價買進+賣出）。
      盤後：限價→預約單可收（user_def 被吃）、市價→4385862 預約單禁市價。
    錯誤碼：4385034 停止融券賣出｜4385715 單價輸入錯誤（沙箱限價全拒）｜
            4385862 預約單禁止市價｜4385022 委託別錯誤｜4385183 不可市價委託"""
    nos = {str(n) for n in (order_nos or ())}
    out = []
    for r in rows or []:
        if dig(r, "user_def") == user_def or str(dig(r, "order_no") or "") in nos:
            out.append(r)
    return out


# ==================== 當沖／融券資格閘門 ====================

def daytrade_info(sdk, acc, symbol):
    """現股當沖資格（daytrade_and_stock_info）。

    回傳 dict：ok=查詢成功與否、quota=可當沖額度、status=後檯狀態碼、raw=原物件。
    實測（SDK 2.2.8 沙箱）：額度一律 0（沙箱不發當沖額度）、DayTrade 委託別被
    後檯拒絕（'委託別錯誤'）→ 沙箱無法現股當沖；正式帳號才會有真實額度。
    """
    try:
        res = sdk.stock.daytrade_and_stock_info(acc, symbol)
        d = dig(res, "data")
        ok = bool(dig(res, "is_success", True))
        return {"ok": ok and d is not None,
                "quota": dig(d, "daytrade_tradable_quota"),
                "orig_quota": dig(d, "daytrade_orig_quota"),
                "status": dig(d, "status"),
                "raw": d}
    except Exception as e:
        return {"ok": False, "quota": None, "status": None,
                "raw": None, "error": str(e)[:120]}


def shortsell_info(sdk, acc, symbol):
    """融券（先賣後買）額度（margin_quota）。

    回傳 dict：ok、shortsell=可融券賣出股數、margin=可融資買進股數、raw。
    實測（沙箱）：data=None（沙箱不發融券額度）→ verdict=unknown；
    正式帳號回真實額度。實盤拒件例：'停止融券賣出(後檯)'。
    """
    try:
        res = sdk.stock.margin_quota(acc, symbol)
        d = dig(res, "data")
        ss = None
        for k in ("shortsell_quota", "short_sell_quota", "shortsell", "sbl_quota"):
            v = dig(d, k)
            if v is not None:
                ss = v
                break
        mg = None
        for k in ("margin_quota", "margin", "financing_quota"):
            v = dig(d, k)
            if v is not None:
                mg = v
                break
        return {"ok": bool(dig(res, "is_success", True)) and d is not None,
                "shortsell": ss, "margin": mg, "raw": d}
    except Exception as e:
        return {"ok": False, "shortsell": None, "margin": None,
                "raw": None, "error": str(e)[:120]}


def entry_gate(sdk, acc, symbol, direction, lenient_zero=None):
    """進場資格預檢。

    回傳 (verdict, reason, detail)：
      verdict ∈ "ok"（可進場）/ "blocked"（明確不可）/ "unknown"（查不到→放行＋警告）
    規則：
      short 方向（融券賣出）→ 需 shortsell 額度 > 0；查不到額度=unknown
      long  方向（現股先買後賣）→ 需 daytrade_tradable_quota > 0；0=blocked（正式帳號）
    lenient_zero：額度=0 時是否降級為 unknown。
      None（預設）＝自動：測試沙箱 → lenient（沙箱不發當沖額度，quota=0 無證據力，
      實測 30 檔全部 0）；正式帳號 → 嚴格（quota=0＝真的不能當沖）。
      回測場景沙箱應放行（模擬不吃沙箱額度限制）；live 下單預設嚴格（沙箱
      DayTrade 委託別會被後檯拒絕，避免進場後出不了場）。
    """
    if lenient_zero is None:
        try:
            lenient_zero = fubon_cfg()["use_test"]
        except Exception:
            lenient_zero = False
    d = {"symbol": symbol, "direction": direction}
    if direction in ("short", "both"):
        m = shortsell_info(sdk, acc, symbol)
        d["shortsell"] = m.get("shortsell")
        if m.get("ok") and m.get("shortsell") is not None:
            try:
                if float(m["shortsell"]) <= 0:
                    if lenient_zero:
                        d["note"] = "沙箱不發融券額度→照跑"
                        return "unknown", "E_GATE_SANDBOX", d
                    return "blocked", "E_NO_SHORT_QUOTA", d
                return "ok", "E_GATE_OK", d
            except (TypeError, ValueError):
                pass
        return "unknown", "E_GATE_UNKNOWN", d
    if direction == "long":
        t = daytrade_info(sdk, acc, symbol)
        d["daytrade_quota"] = t.get("quota")
        if t.get("ok") and t.get("quota") is not None:
            try:
                if float(t["quota"]) <= 0:
                    if lenient_zero:
                        d["note"] = "沙箱不發當沖額度（quota=0 無證據力）→ 照跑"
                        return "unknown", "E_GATE_SANDBOX", d
                    return "blocked", "E_NOT_DAYTRADE", d
                return "ok", "E_GATE_OK", d
            except (TypeError, ValueError):
                pass
        return "unknown", "E_GATE_UNKNOWN", d
    return "unknown", "E_GATE_UNKNOWN", d


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

# 共用登入 session（多檔回測/引擎只登入一次）
_SDK_SESSION = {"key": None, "sdk": None, "acc": None}


def reset_sdk_session():
    """登出並清掉共用 session（網路異常重連、切換環境時用）"""
    try:
        if _SDK_SESSION["sdk"] is not None:
            _SDK_SESSION["sdk"].logout()
    except Exception:
        pass
    _SDK_SESSION.update(key=None, sdk=None, acc=None)


def connect_sdk(cfg, retries=3, reuse=True):
    """登入 SDK（同設定重用 session，避免多檔回測重複登入 30 次）。

    reuse=True：快取 (sdk, Account)；設定變更或 reset_sdk_session() 後重新登入。
    失敗的舊 session：呼叫端捕捉例外後可 reset_sdk_session() 再連。
    """
    key = (cfg["use_test"], cfg["user_id"], cfg["cert_path"])
    if reuse and _SDK_SESSION["sdk"] is not None and _SDK_SESSION["key"] == key:
        return _SDK_SESSION["sdk"], _SDK_SESSION["acc"]
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
            break
        except SystemExit:
            raise
        except Exception as e:
            last_err = e
            log.warning(f"連線異常（第 {attempt}/{retries} 次）：{str(e)[:100]}")
            if attempt < retries:
                time.sleep(2)
    else:
        raise RuntimeError(
            f"網路無法連上富邦主機（DNS／防火牆／VPN 問題，非憑證問題）— 已重試 {retries} 次｜"
            f"最後錯誤：{str(last_err)[:120]}")
    # 選證券帳戶（排除期貨/複委託）——SDK 的 place_order/margin_quota 等
    # 都要求 Account 物件（login 原始回傳是 CustomReturnType，直接用會 TypeError）
    stock_accs = [a for a in (dig(acc, "data") or [])
                  if dig(a, "account_type") in (None, "stock", "")] or list(dig(acc, "data") or [])
    if not stock_accs:
        raise SystemExit("登入成功但找不到可用帳戶")
    sel = stock_accs[0]
    log.info(f"使用證券帳戶：{dig(sel, 'branch_no')}-{dig(sel, 'account')}")
    if reuse:
        _SDK_SESSION.update(key=key, sdk=sdk, acc=sel)
    return sdk, sel


def build_order(Order, BSAction, MarketType, PriceType, TimeInForce, OrderType,
                symbol, side, qty, price, market_flag, odd, tif, ot, user_def):
    """組出富邦新一代 Order 物件。數量一律為股數（1 張 = 1000 股）。"""
    tif_val = {"ROD": TimeInForce.ROD, "IOC": TimeInForce.IOC, "FOK": TimeInForce.FOK}.get(
        (tif or "ROD").upper(), TimeInForce.ROD)
    ot_val = {"stock": OrderType.Stock,
              # SDK 2.2.x：Short / Margin；舊版名稱 ShortSelling / MarginTrading 相容
              "short": getattr(OrderType, "Short",
                               getattr(OrderType, "ShortSelling", OrderType.Stock)),
              "margin": getattr(OrderType, "Margin",
                                getattr(OrderType, "MarginTrading", OrderType.Stock)),
              # 現股當沖（2.2.x 新增；買進+賣出同日標記）
              "daytrade": getattr(OrderType, "DayTrade", OrderType.Stock)}
    is_market = market_flag or not price
    # SDK 2.2.x：市價單 price 必須是 None（空字串會被後檯拒絕：'Price should be empty'）
    return Order(
        buy_sell=BSAction.Buy if side == "buy" else BSAction.Sell,
        symbol=symbol,
        price=None if is_market else str(price),
        quantity=int(qty),
        market_type=MarketType.IntradayOdd if odd else MarketType.Common,
        price_type=PriceType.Market if is_market else PriceType.Limit,
        time_in_force=tif_val,
        order_type=ot_val.get(ot, OrderType.Stock),
        user_def=(user_def or "ORB")[:10],
    )


def tick_size(px):
    """台股 tick 級距（與 strategy.tick_size 同語意；此處獨立實作避免循環匯入）"""
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


def order_band(sdk, acc, symbol):
    """「下單系統」視角的價格帶（官方文件：測試環境判斷可下單價格必用此，
    行情端 intraday.quote 與下單系統的模擬價脫鉤 — 2026-11-06 實測：
    2317 行情 227 vs 下單系統參考價 89.9）。

    ⚠沙箱撮合也在「模擬世界」：2002 限價 21.2 賣出 → 成交價 23.55
    （= 下單系統參考價；行情端同時刻只有 18.5）。即沙箱成交價≠真實行情，
    沙箱只能驗「下單機制」（送單/回報/取消/錯誤路徑），不能驗價格與損益。
    引擎的策略損益一律以真實行情模擬成交計算，與沙箱成交價無關。

    回傳 {"ref","up","down","last"} 或 None（查詢失敗）。
    正式環境：ref=昨收、up/down=真實漲跌停 → 夾帶為無副作用（待 preflight 驗證）。"""
    try:
        q = dig(sdk.stock.query_symbol_quote(acc, symbol), "data")
        raw = dig(q, "raw") or q
        ref = float(dig(raw, "reference_price") or 0)
        up = float(dig(raw, "limitup_price") or 0)
        dn = float(dig(raw, "limitdown_price") or 0)
        last = float(dig(raw, "last_price") or 0)
        if ref <= 0:
            return None
        return {"ref": ref, "up": up, "down": dn, "last": last}
    except Exception:
        return None


def short_entry_price(sdk, acc, symbol, intent_px):
    """放空進場限價：以「下單系統」價格帶為準。

    intent_px＝策略觸發價（真實行情）。邏輯：
      意圖價在帶內（正式環境常態）→ 用意圖價
      意圖價帶外（沙箱：模擬價與真實行情脫鉤）→ 夾進帶內並 tick 對齊
        （未對齊會被拒 4385621；帶外會被拒 4385715）
    回傳 (price, note)；查價失敗回 (None, reason)。"""
    band = order_band(sdk, acc, symbol)
    if not band or not band["down"] or not band["up"]:
        return None, "無法取得下單系統價格帶"
    dn, up = band["down"], band["up"]
    px = float(intent_px or 0)
    inband = dn <= px <= up
    if not inband:
        px = min(max(px, dn), up)
        px = round(math.floor(px / tick_size(px) + 1e-6)
                   * tick_size(px), 2)   # tick 對齊（4385621；+1e-6 防浮點誤差）
    return round(px, 2), ("意圖價帶內" if inband else
                          f"帶外→夾帶 [{dn}~{up}] tick 對齊")


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
                "short": "OrderType.Short（融券賣出/先賣後買）",
                "margin": "OrderType.Margin（融資買進）",
                "daytrade": "OrderType.DayTrade（現股當沖）"}[ot]
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
    ot_name = {"stock": "OrderType.Stock", "short": "OrderType.Short",
               "margin": "OrderType.Margin",
               "daytrade": "OrderType.DayTrade"}[args.ot]

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


def ensure_realtime(sdk):
    """初始化行情（idempotent）：呼叫 init_realtime() 並確認 sdk.marketdata 就緒。

    實測（SDK 2.2.8）：init_realtime() 回傳 None 但會「生出」sdk.marketdata
    （rest_client + websocket_client）；憑證登入即可，不需 API Key。
    成功回傳 sdk.marketdata，失敗回傳 None。
    """
    try:
        if getattr(sdk, "marketdata", None) is None:
            sdk.init_realtime()
        return getattr(sdk, "marketdata", None)
    except Exception:
        return None


def fetch_intraday_trades(sdk, symbol, limit=500):
    """逐筆成交（內外盤原料）：marketdata REST intraday.trades。

    實測回應：data = [{'bid', 'ask', 'price', 'size'(本筆量), 'volume'(累積),
    'time'(微秒), 'serial'}, ...]
    回傳 orderflow 相容 ticks：[{'price', 'volume'(=size), 'bid', 'ask'}, ...]
    """
    md = ensure_realtime(sdk)
    if md is None:
        return []
    try:
        r = md.rest_client.stock.intraday.trades(symbol=str(symbol), limit=int(limit))
        d = dig(r, "data")
        items = d if isinstance(d, list) else (dig(d, "tick") or [])
    except Exception:
        return []
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        try:
            out.append({"price": it.get("price"),
                        "volume": it.get("size") or it.get("volume") or 0,
                        "bid": it.get("bid"),
                        "ask": it.get("ask")})
        except Exception:
            continue
    return out


def fetch_intraday_bars(sdk, symbol, timeframe="5"):
    """最新交易日的 5 分K（富邦行情；自動 init_realtime）"""
    md = ensure_realtime(sdk)
    if md is None:
        return []
    res = md.rest_client.stock.intraday.candles(symbol=symbol, timeframe=timeframe)
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
    # 不登出：session 由 connect_sdk 快取重用（多檔一次登入）；需要時用 reset_sdk_session()
    return bars


if __name__ == "__main__":
    main()
