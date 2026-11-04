# -*- coding: utf-8 -*-
"""
live.py — 盤中自動執行引擎（佛系當沖法）
==========================================
在 Flask 進程內以背景執行緒運行，每個週期（預設 20 秒）：
  抓今日 5 分K（yfinance 或 富邦SDK 行情）
    → strategy.run_day（四階段規則＋核准的避坑規則）
    → 事件差異偵測（ENTRY／STOP_UPDATE／EXIT／RULE_BLOCK）
    → 訊號入庫（signals_log）＋ 完成交易入庫（trades，run_id=LIVE-日期）
    → mode=test 時自動透過富邦沙箱送單（進場放空／出場買回，orders 表）
    → mode=dry  只記錄不送單（預設，零風險）

安全設計：
  - 預設 dry 模式；test 模式需已設定 FUBON_TEST_* 憑證
  - 13:20 時間出場由策略規則觸發（X_TIME_1320 → 買回）
  - 出場一次即止（不反手）、同一事件只處理一次（去重）
  - 重啟 app 後引擎停止（狀態不持久，需重新啟動）
"""
import os
import threading
from datetime import datetime, time as dtime

from logutil import get_logger, ensure_utf8_stdout

ensure_utf8_stdout()

import db as tdb            # noqa: E402
import strategy             # noqa: E402
import fubon_orders as fo   # noqa: E402
import chips as chips_mod   # noqa: E402

log = get_logger("live")


class LiveEngine:
    def __init__(self):
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self.thread = None
        self._seen = set()       # (symbol, ts, reason) 已處理事件
        self._sent = set()       # (symbol, date, kind) 已送單
        self._recorded = set()   # (symbol, date) 已入庫交易
        self.of_rest = None      # 內外盤 REST client（惰性建立；需 FUBON_API_KEY）
        self.of_trackers = {}    # symbol → OrderFlowTracker
        self._of_seen = set()    # (symbol, minute, kind) 已記錄訊號
        self.state = {
            "running": False, "mode": "dry", "source": "yf", "symbols": [],
            "risk_budget": 0.0, "lots": 1, "interval": 20, "gate": True,
            "auto_risk": True, "auto_min_n": 10, "auto_gap": 0.2,
            "auto_rules": [], "auto_rules_sig": None, "auto_mined_at": 0,
            "started_at": None, "last_check": None, "last_error": None,
            "cycles": 0, "events": [], "open_pos": {},
            "of_on": False, "of_signals": [],
        }

    # ---------- 對外控制 ----------
    def start(self, symbols, mode="dry", source="yf", risk_budget=0.0,
              lots=1, interval=20, gate=True, auto_risk=True,
              auto_min_n=10, auto_gap=0.2, direction="short"):
        with self.lock:
            if self.state["running"]:
                return False, "引擎已在執行中"
            if not symbols:
                return False, "請先設定標的"
            self.state.update({
                "running": True, "mode": mode, "source": source,
                "direction": direction if direction in ("short", "long", "both")
                else "short",
                "symbols": symbols, "risk_budget": float(risk_budget or 0),
                "lots": max(1, int(lots or 1)),
                "interval": max(5, int(interval or 20)), "gate": bool(gate),
                "auto_risk": bool(auto_risk),
                "auto_min_n": max(4, int(auto_min_n or 10)),
                "auto_gap": float(auto_gap or 0.2),
                "started_at": datetime.now().strftime("%H:%M:%S"),
                "last_error": None, "cycles": 0})
            self._seen, self._sent, self._recorded = set(), set(), set()
            self.of_trackers, self._of_seen = {}, set()
            self.state["of_signals"] = []
            # 內外盤觀察模式：.env 有 FUBON_API_KEY 才啟用（REST 逐筆，僅記錄）
            try:
                import orderflow as _ofm
                self.state["of_on"] = _ofm.has_api_key()
            except Exception:
                self.state["of_on"] = False
            self.of_rest = None
            # 回測虧損黑名單警告（顯示用；是否排除由使用者決定）
            try:
                import sqlite3 as _sq
                _dbp = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "data", "trades.db")
                if os.path.exists(_dbp):
                    _c = _sq.connect(_dbp)
                    for s_ in symbols:
                        r = _c.execute(
                            "SELECT COUNT(*) n, SUM(net_pnl) s FROM trades "
                            "WHERE symbol=? AND run_id NOT LIKE 'DEMO%'", (s_,)).fetchone()
                        if r and r[0] and (r[1] or 0) < 0:
                            log.warning(f"⚠ {s_} 在回測虧損黑名單（{r[0]} 筆 淨 {r[1]:+,.0f}）")
                    _c.close()
            except Exception:
                pass
            self._stop.clear()
            self.thread = threading.Thread(target=self._loop, daemon=True)
            self.thread.start()
            log.info(f"自動執行啟動：mode={mode} source={source} direction={direction} "
                     f"symbols={symbols} gate={gate} interval={interval}s")
            return True, "已啟動"

    def stop(self):
        with self.lock:
            if not self.state["running"]:
                return False, "引擎未在執行"
            self._stop.set()
            self.state["running"] = False
        log.info("自動執行已停止")
        return True, "已停止"

    def log_event(self, sym, kind, detail, reason=""):
        self.state["events"].insert(0, {"time": datetime.now().strftime("%H:%M:%S"),
                                        "symbol": sym, "kind": kind,
                                        "reason": reason, "detail": detail})
        del self.state["events"][200:]

    # ---------- 背景迴圈 ----------
    def _loop(self):
        while not self._stop.is_set():
            try:
                self._cycle()
            except Exception as e:
                self.state["last_error"] = str(e)[:200]
                log.error(f"live cycle 錯誤：{e}", exc_info=True)
            self._stop.wait(self.state["interval"])

    def _in_session(self):
        if not self.state["gate"]:
            return True
        now = datetime.now()
        if now.weekday() >= 5:
            return False
        return dtime(8, 45) <= now.time() <= dtime(13, 35)

    def _gate_close_positions(self):
        """閘門關閉（13:35 後）仍有未平倉 → 以最後已知價格強制結算入庫。
        原因：yf 延遲 15-20 分，13:20 的 K 棒在 13:40 後才出現，
        但閘門 13:35 已關 → 時間出場永遠模擬不到 → 持倉卡在記憶體。"""
        if not self.state["open_pos"]:
            return
        conn = tdb.connect()
        try:
            for sym, p in list(self.state["open_pos"].items()):
                entry = float(p["entry"])
                exit_price = float(p["last"])
                shares = int(p["lots"]) * 1000
                direction = p.get("direction", "short")
                if direction == "long":
                    gross = (exit_price - entry) * shares
                    tax = exit_price * shares * 0.0015   # 現股：稅在賣出（出場）腿
                else:
                    gross = (entry - exit_price) * shares
                    tax = entry * shares * 0.0015        # 券賣：稅在進場（賣出）腿
                fee_b = max(20, entry * shares * 0.001425 * 0.3)
                fee_s = max(20, exit_price * shares * 0.001425 * 0.3)
                net = round(gross - fee_b - fee_s - tax, 2)
                trade = {
                    "date": p["date"], "symbol": sym, "direction": direction,
                    "trigger_time": p["entry_time"], "trigger_open": entry,
                    "entry_time": p["entry_time"], "entry_price": entry,
                    "initial_stop": p["stop"], "final_stop": p["stop"],
                    "stop_update_count": 0,
                    "exit_time": "13:35", "exit_reason": "X_GATE_CLOSE",
                    "exit_price": exit_price,
                    "lots": int(p["lots"]), "shares": shares,
                    "gross_pnl": round(gross, 2),
                    "fees": round(fee_b + fee_s, 2), "tax": round(tax, 2),
                    "net_pnl": net, "risk_per_share": None, "r_multiple": None,
                    "bars_held": 1, "entry_reason": "E_GATE_RECOVER",
                    "result_tag": strategy.classify("X_GATE_CLOSE", net),
                    "features": str({"note": "閘門收尾：延遲資料未及時模擬 13:20 出場"}),
                    "events": [], "stop_updates": [], "stop": p["stop"],
                }
                tid = tdb.record_trade(conn, trade)
                log.info(f"[live][{sym}] 閘門收尾入庫 #{tid}：出 {exit_price}"
                         f" 淨 {net:+,.0f}（X_GATE_CLOSE）")
                self.state["open_pos"].pop(sym, None)
        finally:
            conn.close()

    def _orderflow_check(self, sym):
        """內外盤觀察模式：REST 逐筆 → tracker → 訊號只記錄（不下單）。
        需 .env 有 FUBON_API_KEY；任何失敗都不影響主迴圈。"""
        if not self.state.get("of_on"):
            return
        try:
            import orderflow as ofm
            if self.of_rest is None:
                self.of_rest = ofm.build_rest_client()
            if self.of_rest is None:
                self.state["of_on"] = False
                log.warning("內外盤觀察停用：REST client 建立失敗（檢查 API Key/套件）")
                return
            tr = self.of_trackers.get(sym)
            if tr is None:
                tr = self.of_trackers[sym] = ofm.OrderFlowTracker()
            ticks = ofm.fetch_ticks_rest(self.of_rest, sym)
            if ticks:
                tr.feed(ticks)
            sig = tr.signal()
            if sig:
                key = (sym, datetime.now().strftime("%Y%m%d%H%M"), sig["kind"])
                if key not in self._of_seen:
                    self._of_seen.add(key)
                    self.state["of_signals"].insert(
                        0, {"time": datetime.now().strftime("%H:%M:%S"),
                            "symbol": sym, "kind": sig["kind"],
                            "detail": sig["detail"]})
                    del self.state["of_signals"][40:]
                    self.log_event(sym, "OF_SIGNAL", str(sig["detail"])[:160],
                                   f"E_OF_{sig['kind']}")
                    log.info(f"[內外盤][{sym}] {sig['kind']} {sig['detail']}")
        except Exception as e:
            log.warning(f"內外盤觀察失敗（{sym}）：{str(e)[:120]}")

    def _cycle(self):
        if not self.state["symbols"]:
            return
        if self.state["gate"] and not self._in_session():
            self.state["last_check"] = datetime.now().strftime("%H:%M:%S") + "（非交易時段）"
            self._gate_close_positions()
            return
        cfg = {**strategy.DEFAULTS,
               "risk_budget": self.state["risk_budget"], "lots": self.state["lots"],
               "eod_exit": False}   # live：盤中資料未收尾不自動平倉
        conn = tdb.connect()
        try:
            rules = [dict(r) for r in tdb.list_rules(conn, approved_only=True)]
            if self.state["auto_risk"]:
                self._refresh_auto_rules(conn)
                rules += list(self.state["auto_rules"])
        finally:
            conn.close()
        self.state["cycles"] += 1
        direction = self.state.get("direction", "short")
        for sym in self.state["symbols"]:
            if self.state.get("of_on"):
                self._orderflow_check(sym)   # 內外盤觀察（失敗不影響主流程）
            bars = self._fetch_today(sym)
            if not bars:
                continue
            date = bars[0]["date"]
            if direction == "both":
                # 多空雙向：兩套模擬都跑，取先觸發者（同根K不會同時長黑與長紅）
                res_s = strategy.run_day(bars, {**cfg, "direction": "short"},
                                         sym, date, log=log, rules=rules)
                res_l = strategy.run_day(bars, {**cfg, "direction": "long"},
                                         sym, date, log=log, rules=rules)
                ts_, tl_ = res_s.get("trade"), res_l.get("trade")
                if ts_ and tl_:
                    res = (res_s if (ts_.get("entry_time") or "")
                           <= (tl_.get("entry_time") or "") else res_l)
                elif ts_:
                    res = res_s
                elif tl_:
                    res = res_l
                else:
                    res = {"trade": None,
                           "signals": list(res_s.get("signals") or [])
                           + list(res_l.get("signals") or [])}
            else:
                res = strategy.run_day(bars, {**cfg, "direction": direction},
                                       sym, date, log=log, rules=rules)
            self._handle(sym, date, res, bars)

    def _refresh_auto_rules(self, conn):
        """自動風控：從累積交易數據重挖規則（30 分鐘一次或樣本成長時）；
        證據消失的規則自動失效＝自我修正"""
        import time as _time
        import journal as jr
        now = _time.time()
        n_trades = conn.execute("SELECT COUNT(*) c FROM trades").fetchone()["c"]
        if (self.state.get("auto_rules_sig") is not None
                and now - (self.state.get("auto_mined_at") or 0) < 1800):
            return
        try:
            found = jr.mine_auto_rules(conn, min_n=self.state["auto_min_n"],
                                       gap=self.state["auto_gap"])
        except Exception as e:
            log.error(f"自動風控挖掘失敗：{e}")
            return
        new_sig = tuple(sorted(r["name"] for r in found))
        old_sig = self.state.get("auto_rules_sig")
        self.state["auto_rules"] = found
        self.state["auto_rules_sig"] = new_sig
        self.state["auto_mined_at"] = now
        if new_sig != old_sig:
            old_set = set(old_sig or ())
            added = [r["name"] for r in found if r["name"] not in old_set]
            removed = [n for n in (old_sig or ()) if n not in set(new_sig)]
            log.info(f"自動風控規則更新：{len(found)} 條生效（交易樣本 {n_trades}）"
                     f"｜新增 {len(added)}｜失效 {len(removed)}")
            self.log_event("系統", "AUTO_RULE",
                           f"生效 {len(found)} 條｜新增 {added}｜失效 {removed}", "E_AUTO_RULE")
            try:
                import notify
                notify.send(f"🛡 自動風控規則更新（生效 {len(found)} 條）\n"
                            + "\n".join(f"• {r['name']}｜{r['evidence']}" for r in found[:8]))
            except Exception:
                pass

    # ---------- 事件處理 ----------
    def _handle(self, sym, date, res, bars):
        events = list(res["signals"] or [])
        t = res["trade"]
        if t:
            events = list(t["events"]) + events
        for ev in events:
            key = (sym, ev.get("ts"), ev.get("reason"))
            if key in self._seen:
                continue
            self._seen.add(key)
            kind, reason = ev.get("event", "?"), ev.get("reason", "")
            self.log_event(sym, kind, str(ev.get("detail", ""))[:160], reason)
            log.info(f"[live][{sym}] {kind} {reason} @ {ev.get('ts')}")
            # Telegram 通知（未設定 Token 時自動略過）
            if kind in ("ENTRY", "EXIT", "ORDER", "ORDER_FAIL", "RULE_BLOCK"):
                try:
                    import notify
                    notify.send(f"⚡佛系當沖 [{sym}] {kind}（{reason}）\n"
                                f"{str(ev.get('detail', ''))[:200]}")
                except Exception:
                    pass
            # 未成形交易的事件（規則擋單等）入 signals_log
            if not t or reason in ("E_RULE_BLOCK", "E_NO_NEXT_BAR"):
                conn = tdb.connect()
                try:
                    tdb.record_signal(conn, date, sym, ev.get("ts", ""), kind,
                                      reason, str(ev.get("detail", "")))
                finally:
                    conn.close()
            # 自動下單（僅 test 模式；dry 只記錄）
            if self.state["mode"] == "test":
                _t_dir = (t.get("direction") if isinstance(t, dict) else None) \
                    or self.state.get("direction", "short")
                if kind == "ENTRY" and (sym, date, "entry") not in self._sent:
                    self._sent.add((sym, date, "entry"))
                    if _t_dir == "long":
                        self._order(sym, "buy", "stock", "進場買進")
                    else:
                        self._order(sym, "sell", "short", "進場放空")
                    # 進階保護：同步下「停損買回條件單」（環境變數開關，預設關）
                    if os.environ.get("LIVE_AUTO_CONDITION") == "1":
                        try:
                            import fubon_condition as fc
                            sdk2, acc2 = self._ensure_sdk()
                            cres, cguid, cok = fc.place_stop(
                                sdk2, acc2, sym, t["shares"], t["stop"], log=log)
                            self.log_event(sym, "CONDITION",
                                           f"停損條件單 guid={cguid} trigger={t['stop']} ok={cok}")
                        except Exception as ce:
                            log.error(f"[live][{sym}] 停損條件單建立失敗：{ce}")
                elif kind == "EXIT" and (sym, date, "exit") not in self._sent:
                    self._sent.add((sym, date, "exit"))
                    if _t_dir == "long":
                        self._order(sym, "sell", "stock", f"出場賣出（{reason}）")
                    else:
                        self._order(sym, "buy", "stock", f"出場買回（{reason}）")
        # 完成交易入庫（每 (標的,日期) 一次）
        if t and t.get("exit_price") is not None and (sym, date) not in self._recorded:
            self._recorded.add((sym, date))
            t2 = dict(t)
            t2["run_id"] = f"LIVE-{date}"
            conn = tdb.connect()
            try:
                tid = tdb.record_trade(conn, t2)
                log.info(f"[live][{sym}] 交易 #{tid} 已入庫（淨 {t['net_pnl']:+,.0f}，"
                         f"標籤 {t['result_tag']}）")
            finally:
                conn.close()
        # 持倉監控摘要
        if t and t.get("exit_price") is None:
            last = bars[-1]
            _d = t.get("direction", "short")
            unreal = ((last["close"] - t["entry_price"]) if _d == "long"
                      else (t["entry_price"] - last["close"])) * t["shares"]
            self.state["open_pos"][sym] = {
                "date": date, "entry_time": t["entry_time"], "entry": t["entry_price"],
                "stop": t["stop"], "lots": t["lots"], "last": last["close"],
                "direction": _d, "unreal": unreal}
        else:
            self.state["open_pos"].pop(sym, None)
        self.state["last_check"] = datetime.now().strftime("%H:%M:%S")

    # ---------- 下單（test 沙箱） ----------
    MY_ORDER_TAG = "FSDT"    # user_def 標記：共用測試帳號下辨別自己的單

    def _ensure_sdk(self):
        if self.state.get("sdk") is None:
            cfg = fo.fubon_cfg()
            try:
                sdk, acc = fo.connect_sdk(cfg)
                try:
                    sdk.init_realtime()   # 行情連線（marketdata 需要）
                except Exception:
                    pass
                self.state["sdk"], self.state["acc"] = sdk, acc
            except SystemExit as e:  # 登入失敗 → 轉一般例外，避免執行緒靜默死亡
                raise RuntimeError(str(e))
        return self.state["sdk"], self.state["acc"]

    def _order(self, sym, side, ot, note):
        try:
            sdk, acc = self._ensure_sdk()
            # 當沖/融券資格預檢（僅進場單；出場一律放行；FUBON_GATE=0 可關閉）
            if (note.startswith("進場")
                    and os.environ.get("FUBON_GATE", "1") != "0"):
                verdict, greason, gd = fo.entry_gate(
                    sdk, acc, sym, "long" if side == "buy" else "short")
                if verdict == "blocked":
                    log.warning(f"[live][{sym}] 進場擋下（{greason}）：{gd}")
                    self.log_event(sym, "GATE_BLOCK",
                                   f"進場資格不符（{greason}）｜{str(gd)[:120]}",
                                   greason)
                    self.state["last_error"] = f"{sym} 進場擋下：{greason}"
                    return
                if verdict == "unknown":
                    log.info(f"[live][{sym}] 進場資格查不到（沙箱常態）→ 照常送單")
            from fubon_neo.sdk import Order
            from fubon_neo.constant import (BSAction, MarketType, PriceType,
                                            TimeInForce, OrderType)
            order = fo.build_order(Order, BSAction, MarketType, PriceType, TimeInForce,
                                   OrderType, sym, side, 1000, None, True, False,
                                   "ROD", ot, self.MY_ORDER_TAG)
            res = sdk.stock.place_order(acc, order)
            data = fo.dig(res, "data")
            no = fo.dig(data, "order_no") if data else None
            ok = bool(fo.dig(res, "is_success", True))
            log.info(f"[live][{sym}] {note} 送單 order_no={no} ok={ok} "
                     f"user_def={self.MY_ORDER_TAG}")
            if no:   # 記住自己的委託書號（共用沙箱帳號下，查單只認自己的）
                self.state.setdefault("my_orders", []).append(
                    {"order_no": str(no), "symbol": sym, "side": side,
                     "time": datetime.now().strftime("%H:%M:%S")})
                del self.state["my_orders"][:-50]
            conn = tdb.connect()
            try:
                tdb.record_order(conn, mode="test", symbol=sym, side=side,
                                 order_type=ot, quantity=1000, price="",
                                 price_type="Market", market_type="Common", tif="ROD",
                                 user_def="FSDT", order_no=str(no) if no else None,
                                 is_success=int(ok), message=f"live {note}", raw="")
            finally:
                conn.close()
            self.log_event(sym, "ORDER", f"{note} order_no={no} ok={ok}")
        except Exception as e:
            log.error(f"[live][{sym}] 送單失敗：{e}", exc_info=True)
            self.log_event(sym, "ORDER_FAIL", str(e)[:160])
            self.state["last_error"] = f"送單失敗：{e}"

    def my_order_results(self):
        """只回自己的委託回報（共用測試帳號防混）：自記 order_no 或 user_def 標記"""
        sdk, acc = self._ensure_sdk()
        rows = fo.dig(sdk.stock.get_order_results(acc), "data") or []
        my_nos = {o.get("order_no") for o in self.state.get("my_orders", [])}
        out = [r for r in rows
               if (fo.dig(r, "user_def") == self.MY_ORDER_TAG
                   or str(fo.dig(r, "order_no")) in my_nos)]
        return out

    # ---------- 行情來源 ----------
    def _fetch_today(self, sym):
        try:
            if self.state["source"] == "fubon":
                return self._fetch_fubon(sym)
            return self._fetch_yf(sym)
        except Exception as e:
            self.state["last_error"] = f"{sym} 行情取得失敗：{str(e)[:120]}"
            return []

    def _fetch_fubon(self, sym):
        sdk, acc = self._ensure_sdk()
        return fo.fetch_intraday_bars(sdk, sym)

    def _fetch_yf(self, sym):
        import time
        import yfinance as yf
        try:
            import ticker_db
            cands = ticker_db.yf_candidates(sym)   # 本地對照表→正確後綴優先
        except Exception:
            cands = [sym] if "." in sym else [f"{sym}.TW", f"{sym}.TWO"]
        for s in cands:
            df = None
            for attempt in range(2):    # Yahoo 偶發 Timeout → 稍候重試一次
                try:
                    df = yf.download(s, period="1d", interval="5m",
                                     progress=False, auto_adjust=False)
                except Exception:
                    df = None
                if df is not None and not df.empty:
                    break
                if attempt == 0:
                    time.sleep(1.5)
            if df is None or df.empty:
                continue
            if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
                df.columns = df.columns.get_level_values(0)
            bars = []
            for idx, r in df.iterrows():
                try:
                    o, h, l, c = (float(r["Open"]), float(r["High"]),
                                  float(r["Low"]), float(r["Close"]))
                except (TypeError, ValueError):
                    continue
                if any(x != x for x in (o, h, l, c)):
                    continue
                idx2 = (idx.tz_localize("Asia/Taipei") if idx.tzinfo is None
                        else idx.tz_convert("Asia/Taipei"))
                ts = idx2.to_pydatetime()
                bars.append({"date": ts.strftime("%Y-%m-%d"), "hhmm": ts.strftime("%H:%M"),
                             "time": ts.strftime("%Y-%m-%d %H:%M"),
                             "open": round(o, 2), "high": round(h, 2),
                             "low": round(l, 2), "close": round(c, 2),
                             "volume": float(r.get("Volume", 0) or 0)})
            if bars:
                return bars
        return []


def suggest_symbols():
    """建議標的：多空分類（做空+偏空）→ 盤前清單 → 籌碼候選 → 掃描器推薦股"""
    import glob
    base = os.path.dirname(os.path.abspath(__file__))
    # ⓪ 多空分類清單（使用者 tw_stock.db：做空+偏空，做空優先、越空越前）
    try:
        import longshort
        _, syms = longshort.short_symbols()
        if syms:
            return syms
    except Exception:
        pass
    # ① 最新盤前觀察清單（chips.py --watchlist 產生；取最新一份，不限當日）
    wls = sorted(glob.glob(os.path.join(base, "data", "watchlist_*.txt")))
    if wls:
        codes = []
        for ln in open(wls[-1], encoding="utf-8"):
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                codes.append(ln.split()[0])
        if codes:
            return ",".join(codes[:8])
    # ② 籌碼候選
    out = []
    try:
        D = chips_mod.load_dataset()
        out = [info["code"] for _, info in chips_mod.daytrade_short_candidates(D, 50)][:5]
    except Exception:
        out = []
    if not out:
        # ③ 掃描器推薦股
        try:
            path = os.path.join(base, "data", "daytrade_candidates_hot.txt")
            with open(path, encoding="utf-8") as f:
                out = [ln.split()[0] for ln in f
                       if ln.strip() and not ln.startswith("#")][:5]
        except Exception:
            out = []
    return ",".join(out)


engine = LiveEngine()


def print_suggestions(brief=False):
    """列印目前建議標的與來源（CLI 檢視＋start_web*.bat 啟動時顯示）"""
    print("=" * 60)
    print("⚡ 自動執行引擎 — 建議標的")
    print("=" * 60)
    try:
        import longshort as _ls
        _d, _rows = _ls.load_longshort()
    except Exception:
        _d, _rows = None, []
    if _rows:
        _n1 = sum(1 for r in _rows if r["bias_label"] == "做空")
        _n2 = sum(1 for r in _rows if r["bias_label"] == "偏空")
        print(f"🧭 來源：多空分類清單（基準日 {_d}）｜做空 {_n1} 檔＋偏空 {_n2} 檔")
        for r in _rows:
            print(f"  {r['stock_no']}  {r['bias_label']}  score={r['bias_score']:>4}"
                  f"  close={r['close']}  {'/'.join(r['tags'][:4])}")
    else:
        print("（多空清單無資料 → 後備來源：盤前觀察清單／籌碼候選／掃描器推薦）")
    print("-" * 60)
    print(f"suggest_symbols()：{suggest_symbols()}")
    if not brief:
        print("-" * 60)
        print("⚠ live.py 是引擎模組（供 Flask 載入），無法由此直接啟動交易。")
        print("  啟動方式：開網頁 http://127.0.0.1:9757/live → 表單已預填上列標的 → 啟動")
        print("  或執行 start_web_forever.bat 後再進 /live 頁")


if __name__ == "__main__":
    import sys
    print_suggestions(brief="--brief" in sys.argv)
