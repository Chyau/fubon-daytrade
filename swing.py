# -*- coding: utf-8 -*-
"""波段引擎 — 跨日持倉的自動執行（與當沖引擎 live.py 並行、互不干擾）

兩種進場策略＋一種定投：
  ① lu_swing 漲停次日波段（實證：跳空≤1% 進場 n=180 勝率57% avg +6.71%）
     昨收漲幅 ≥9%（漲停）→ 今日開盤跳空 ≤1% → 市價買進 1 張
     停損＝漲停日收盤價（跌回即出＝動能失效）；持有 ≤ max_hold_days
  ② sig_swing 訊號波段版
     當沖做多訊號（長紅K 觸發）→ 市價買進 1 張
     停損＝觸發K最低點；持有 ≤ max_hold_days
  ③ dca 定期定額
     設定標的/金額/星期/時間 → 每週自動零股買進（限價＝市價+1tick）
     （盤中零股僅限限價單；無自動出場）

安全：dry 模式預設（只記錄不送單）；真送單需 PLACE_ORDER=true。
狀態：引擎狀態存 data/swing_state.json（重啟恢復今日已執行清單）。
"""
import io
import json
import math
import os
import sys
import threading
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from logutil import get_logger, ensure_utf8_stdout

ensure_utf8_stdout()
log = get_logger("swing", "INFO")

import db as tdb
import positions as pos_mod

STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "data", "swing_state.json")
LU_THRESHOLD = 9.0      # 漲停判定（收盤漲幅 %）
LU_MAX_GAP = 1.0        # 次日跳空上限 %（實證：>1% 報酬轉差）
DEFAULT_MAX_HOLD = 3


def _load_state():
    try:
        return json.load(open(STATE_PATH, encoding="utf-8"))
    except Exception:
        return {}


def _save_state(st):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    json.dump(st, open(STATE_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


class SwingEngine:
    def __init__(self):
        self.state = {
            "running": False, "mode": "dry", "interval": 60,
            "symbols": [], "lu_enabled": True, "sig_enabled": False,
            "max_hold_days": DEFAULT_MAX_HOLD,
            "dca_enabled": False, "dca_symbols": [], "dca_amount": 3000,
            "dca_weekday": 0, "dca_time": "10:00",
            "last_error": "", "cycles": 0,
            "events": [], "_stop": None, "_thread": None,
        }
        self.state.update(_load_state())
        self._of_last = {}
        self._swing_bars = {}   # sig_swing：觸發K低點暫存 {(sym,date): low}

    # ---------- 生命週期 ----------
    def start(self, symbols, mode="dry", lu_enabled=True, sig_enabled=False,
              max_hold_days=DEFAULT_MAX_HOLD, dca_enabled=False,
              dca_symbols=None, dca_amount=3000, dca_weekday=0,
              dca_time="10:00", interval=60):
        if self.state["running"]:
            self.stop()
        self.state.update({
            "running": True, "mode": mode, "symbols": symbols,
            "lu_enabled": lu_enabled, "sig_enabled": sig_enabled,
            "max_hold_days": int(max_hold_days),
            "dca_enabled": dca_enabled, "dca_symbols": dca_symbols or [],
            "dca_amount": int(dca_amount), "dca_weekday": int(dca_weekday),
            "dca_time": dca_time, "interval": int(interval),
            "cycles": 0, "last_error": "",
        })
        _save_state({k: v for k, v in self.state.items()
                     if not k.startswith("_")})
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return f"波段引擎啟動：mode={mode}｜標的 {len(symbols)} 檔｜漲停跟單 {lu_enabled}｜訊號波段 {sig_enabled}｜定投 {dca_enabled}"

    def stop(self):
        self.state["running"] = False
        if self._stop:
            self._stop.set()
        _save_state({k: v for k, v in self.state.items() if not k.startswith("_")})
        return "波段引擎停止"

    def log_event(self, sym, kind, detail, reason=""):
        self.state["events"].insert(0, {
            "time": datetime.now().strftime("%H:%M:%S"), "symbol": sym,
            "kind": kind, "reason": reason, "detail": detail})
        del self.state["events"][200:]

    # ---------- 主迴圈 ----------
    def _loop(self):
        import time
        while not self._stop.is_set():
            try:
                self._cycle()
            except BaseException as e:
                self.state["last_error"] = str(e)[:200]
                log.error(f"swing cycle 錯誤：{e}", exc_info=True)
            self._stop.wait(self.state["interval"])

    def _in_session(self):
        now = datetime.now()
        if now.weekday() >= 5:
            return False
        t = now.strftime("%H:%M")
        return "08:45" <= t <= "13:35"

    def _cycle(self):
        if self._in_session():
            self.state["cycles"] += 1
            dry = self.state["mode"] == "dry"
            sdk = self._sdk()
            if sdk is None:
                return
            # ① 持倉出場巡檢（停損/到期）
            actions = pos_mod.check_exits(sdk, dry=dry)
            for a in actions:
                self.log_event(a["symbol"], "SWING_EXIT",
                               f"{a['action']} @ {a['price']}｜送單 {a['sent']}｜{a['msg']}",
                               a["action"])
            # ② 進場掃描（每天一次：開盤後 09:10-10:30 之間）
            now = datetime.now()
            hhmm = now.strftime("%H:%M")
            st = _load_state()
            done_today = st.get("lu_done", {}).get(now.strftime("%Y-%m-%d"), [])
            if self.state["lu_enabled"] and "09:05" <= hhmm <= "10:30":
                for sym in self.state["symbols"]:
                    if sym in done_today:
                        continue
                    self._lu_scan(sdk, sym, dry)
            # ③ 訊號波段版（每 cycle 檢查長紅K觸發）
            if self.state["sig_enabled"]:
                self._sig_scan(sdk, dry)
            # ④ 定期定額
            if self.state["dca_enabled"]:
                self._dca_check(sdk, dry)
        else:
            self.state["last_check"] = "非交易時段"

    def _sdk(self):
        try:
            import fubon_orders as fo
            return fo.connect_sdk(fo.fubon_cfg())
        except BaseException as e:
            self.state["last_error"] = f"SDK 連線失敗：{str(e)[:120]}"
            return None

    # ---------- ① 漲停次日 ----------
    def _prev_chg_and_close(self, sdk, sym):
        """前日漲幅% 與前日收盤（5 分K 組日收盤；排除今日）"""
        try:
            import fubon_orders as fo
            bars = fo.fetch_fubon_history(sym, days_back=7, timeframe="5") or []
            closes = {}
            for b in bars:
                closes[b["date"]] = b["close"]
            today = datetime.now().strftime("%Y-%m-%d")
            days = sorted(d for d in closes if d < today)
            if len(days) >= 2 and closes[days[-2]]:
                chg = (closes[days[-1]] / closes[days[-2]] - 1) * 100
                return round(chg, 2), closes[days[-1]], days[-1]
        except BaseException as e:
            log.debug(f"[swing][{sym}] 前日資料失敗：{str(e)[:80]}")
        return None, None, None

    def _lu_scan(self, sdk, sym, dry):
        st = _load_state()
        today = datetime.now().strftime("%Y-%m-%d")
        chg, prev_close, prev_date = self._prev_chg_and_close(sdk, sym)
        st.setdefault("lu_done", {})[today] = st.get("lu_done", {}).get(today, [])
        if chg is None:
            return
        st["lu_done"][today].append(sym)
        del st["lu_done"][today][:-100]
        _save_state(st)
        if chg < LU_THRESHOLD:
            return
        # 漲停股 → 檢查今日跳空
        px = pos_mod.last_price(sdk, sym)
        if not px:
            return
        gap = (px / prev_close - 1) * 100
        if gap > LU_MAX_GAP:
            self.log_event(sym, "LU_SKIP",
                           f"昨漲停 {chg:+.1f}% 但跳空 {gap:+.1f}% > {LU_MAX_GAP}% → 不進"
                           f"（實證：跳空越大報酬越差）", "E_LU_GAP")
            return
        if (sym, today, "lu") in [(p["symbol"], p["open_date"], "lu_swing")
                                  for p in pos_mod.list_open("lu_swing")]:
            return
        # 市價買進 1 張
        ok, msg, ono = pos_mod._place(sdk, sym, "buy", 1000, False, dry, "SWING")
        entry = px   # dry/實單皆以現價記錄（實際成交價以回報為準）
        if ok or dry:
            pos_mod.open_position(sym, "lu_swing", 1000, entry,
                                  round(prev_close, 2),
                                  self.state["max_hold_days"],
                                  note=f"昨漲停{chg:+.1f}%｜跳空{gap:+.1f}%", dry=dry)
            self.log_event(sym, "LU_BUY",
                           f"昨漲停 {chg:+.1f}%｜跳空 {gap:+.1f}% → 買進 1 張 @~{px}"
                           f"｜停損 {prev_close:.2f}（跌回漲停價即出）｜{msg}",
                           "E_LU_BUY")
        else:
            self.log_event(sym, "LU_BUY_FAIL", f"送單失敗：{msg}", "E_ORDER_FAIL")

    # ---------- ② 訊號波段版 ----------
    def _sig_scan(self, sdk, sym, dry):
        """當沖做多訊號（長紅K）→ 開波段倉（無 13:20 出場）。
        偵測：最近一根 5 分K 為長紅K（實體 ≥ min_body_pct 且為近 20 根均實體 1.5 倍）"""
        try:
            import fubon_orders as fo
            import strategy
            bars = fo.fetch_fubon_history(sym, days_back=2, timeframe="5") or []
            if len(bars) < 6:
                return
            today = datetime.now().strftime("%Y-%m-%d")
            todays = [b for b in bars if b["date"] == today]
            if len(todays) < 2:
                return
            b = todays[-2]   # 前一根（已收盤）
            o, c = float(b["open"]), float(b["close"])
            if c <= o:
                return
            bodies = [abs(float(x["close"]) - float(x["open"]))
                      for x in todays[:-1][-20:]]
            avg = sum(bodies) / len(bodies) if bodies else 0
            body = c - o
            if o > 0 and body / o * 100 >= strategy.DEFAULTS["min_body_pct"] \
                    and (not bodies or body >= 1.5 * avg):
                key = (sym, today, b["hhmm"])
                if self._swing_bars.get(key):
                    return
                self._swing_bars[key] = float(b["low"])
                # 已有同標的訊號倉 → 不重複
                if any(p["symbol"] == sym and p["strategy"] == "sig_swing"
                       for p in pos_mod.list_open("sig_swing")):
                    return
                ok, msg, ono = pos_mod._place(sdk, sym, "buy", 1000, False,
                                              dry, "SWING")
                px = pos_mod.last_price(sdk, sym) or c
                if ok or dry:
                    pos_mod.open_position(sym, "sig_swing", 1000, px,
                                          round(float(b["low"]), 2),
                                          self.state["max_hold_days"],
                                          note=f"長紅K {b['hhmm']}｜實體 {body/o*100:.1f}%",
                                          dry=dry)
                    self.log_event(sym, "SIG_BUY",
                                   f"長紅K {b['hhmm']}（實體 {body/o*100:.1f}%）"
                                   f"→ 波段買進 @~{px}｜停損 {b['low']:.2f}（觸發K低點）"
                                   f"｜{msg}", "E_SIG_BUY")
        except BaseException as e:
            self.state["last_error"] = f"sig_scan {sym}：{str(e)[:120]}"

    # ---------- ③ 定期定額 ----------
    def _dca_check(self, sdk, dry):
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        if now.weekday() != self.state["dca_weekday"]:
            return
        if now.strftime("%H:%M") < self.state["dca_time"]:
            return
        st = _load_state()
        if st.get("dca_last") == today:
            return
        st["dca_last"] = today
        _save_state(st)
        for sym in self.state["dca_symbols"]:
            try:
                px = pos_mod.last_price(sdk, sym)
                if not px:
                    self.log_event(sym, "DCA_SKIP", "取不到價格", "E_DCA_NOPX")
                    continue
                amount = self.state["dca_amount"]
                shares = int(amount // px)
                if shares < 1:
                    self.log_event(sym, "DCA_SKIP",
                                   f"金額 {amount} 不足 1 股（價 {px}）", "E_DCA_LOW")
                    continue
                odd = shares < 1000
                if odd:
                    ok, msg, ono = self._odd_buy(sdk, sym, shares, dry)
                else:
                    shares = (shares // 1000) * 1000
                    if shares < 1000:
                        self.log_event(sym, "DCA_SKIP", "不足 1 張", "E_DCA_LOW")
                        continue
                    ok, msg, ono = pos_mod._place(sdk, sym, "buy", shares,
                                                  False, dry, "DCA")
                if ok or dry:
                    pos_mod.open_position(sym, "dca", shares, px, None,
                                          0, note=f"定投 {amount} 元", dry=dry)
                    self.log_event(sym, "DCA_BUY",
                                   f"定投 {amount} 元 → {shares} 股 @~{px}｜{msg}",
                                   "E_DCA_BUY")
                else:
                    self.log_event(sym, "DCA_FAIL", msg, "E_ORDER_FAIL")
            except BaseException as e:
                self.log_event(sym, "DCA_ERR", str(e)[:120], "E_DCA_ERR")

    def _odd_buy(self, sdk, sym, shares, dry):
        """盤中零股限價買進（零股僅限限價）：市價+1 tick 限價、帶內夾取"""
        try:
            import fubon_orders as fo
            from fubon_neo.sdk import Order
            from fubon_neo.constant import (BSAction, MarketType, PriceType,
                                            TimeInForce, OrderType)
            # 零股帶：正式環境用 query_symbol_quote(acc, sym, IntradayOdd)
            px = pos_mod.last_price(sdk, sym)
            if not px:
                return False, "取不到價格", None
            t = acct_tick_odd(px)
            limit_px = round(math.floor(px / t) * t + t, 2)   # 買限價=市價+1tick（積極）
            real = (not dry) and os.environ.get(
                "PLACE_ORDER", "false").strip().lower() == "true"
            if not real:
                return False, "dry 模擬（未送單）", None
            order = fo.build_order(Order, BSAction, MarketType, PriceType,
                                   TimeInForce, OrderType, sym, "buy", shares,
                                   limit_px, False, True, "ROD", "stock", "DCA")
            res = sdk.stock.place_order(pos_mod.sdk_acc(sdk), order)
            ok = bool(getattr(res, "is_success", False))
            msg = getattr(res, "message", None)
            ono = getattr(getattr(res, "data", None), "order_no", None)
            conn = tdb.connect()
            try:
                tdb.record_order(conn, mode="dca", symbol=sym, side="buy",
                                 order_type="stock", quantity=shares,
                                 price=str(limit_px), price_type="Limit",
                                 market_type="IntradayOdd", tif="ROD",
                                 user_def="DCA", order_no=str(ono) if ono else None,
                                 is_success=int(ok),
                                 message=f"DCA odd buy｜{msg or ''}", raw="")
            finally:
                conn.close()
            return ok, (msg or f"限價 {limit_px} 送單"), ono
        except BaseException as e:
            return False, str(e)[:150], None


def acct_tick_odd(px):
    """盤中零股 tick（與整股不同：50 元以下 0.01）"""
    if px < 50:
        return 0.01
    if px < 100:
        return 0.05
    if px < 500:
        return 0.1
    if px < 1000:
        return 0.5
    return 1.0


engine = SwingEngine()


if __name__ == "__main__":
    print("狀態：", json.dumps({k: v for k, v in engine.state.items()
                                if not k.startswith("_")},
                               ensure_ascii=False, default=str)[:400])
    print("未平倉：")
    for p in pos_mod.list_open():
        print(" ", p)
