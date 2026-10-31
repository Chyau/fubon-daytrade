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
        self.state = {
            "running": False, "mode": "dry", "source": "yf", "symbols": [],
            "risk_budget": 0.0, "lots": 1, "interval": 20, "gate": True,
            "auto_risk": True, "auto_min_n": 10, "auto_gap": 0.2,
            "auto_rules": [], "auto_rules_sig": None, "auto_mined_at": 0,
            "started_at": None, "last_check": None, "last_error": None,
            "cycles": 0, "events": [], "open_pos": {},
        }

    # ---------- 對外控制 ----------
    def start(self, symbols, mode="dry", source="yf", risk_budget=0.0,
              lots=1, interval=20, gate=True, auto_risk=True,
              auto_min_n=10, auto_gap=0.2):
        with self.lock:
            if self.state["running"]:
                return False, "引擎已在執行中"
            if not symbols:
                return False, "請先設定標的"
            self.state.update({
                "running": True, "mode": mode, "source": source,
                "symbols": symbols, "risk_budget": float(risk_budget or 0),
                "lots": max(1, int(lots or 1)),
                "interval": max(5, int(interval or 20)), "gate": bool(gate),
                "auto_risk": bool(auto_risk),
                "auto_min_n": max(4, int(auto_min_n or 10)),
                "auto_gap": float(auto_gap or 0.2),
                "started_at": datetime.now().strftime("%H:%M:%S"),
                "last_error": None, "cycles": 0})
            self._seen, self._sent, self._recorded = set(), set(), set()
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
            log.info(f"自動執行啟動：mode={mode} source={source} "
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

    def _cycle(self):
        if not self.state["symbols"]:
            return
        if self.state["gate"] and not self._in_session():
            self.state["last_check"] = datetime.now().strftime("%H:%M:%S") + "（非交易時段）"
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
        for sym in self.state["symbols"]:
            bars = self._fetch_today(sym)
            if not bars:
                continue
            date = bars[0]["date"]
            res = strategy.run_day(bars, cfg, sym, date, log=log, rules=rules)
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
                if kind == "ENTRY" and (sym, date, "entry") not in self._sent:
                    self._sent.add((sym, date, "entry"))
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
            self.state["open_pos"][sym] = {
                "date": date, "entry_time": t["entry_time"], "entry": t["entry_price"],
                "stop": t["stop"], "lots": t["lots"], "last": last["close"],
                "unreal": (t["entry_price"] - last["close"]) * t["shares"]}
        else:
            self.state["open_pos"].pop(sym, None)
        self.state["last_check"] = datetime.now().strftime("%H:%M:%S")

    # ---------- 下單（test 沙箱） ----------
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
            from fubon_neo.sdk import Order
            from fubon_neo.constant import (BSAction, MarketType, PriceType,
                                            TimeInForce, OrderType)
            order = fo.build_order(Order, BSAction, MarketType, PriceType, TimeInForce,
                                   OrderType, sym, side, 1000, None, True, False,
                                   "ROD", ot, "FSDT")
            res = sdk.stock.place_order(acc, order)
            data = fo.dig(res, "data")
            no = fo.dig(data, "order_no") if data else None
            ok = bool(fo.dig(res, "is_success", True))
            log.info(f"[live][{sym}] {note} 送單 order_no={no} ok={ok}")
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
        import yfinance as yf
        cands = [sym] if "." in sym else [f"{sym}.TW", f"{sym}.TWO"]
        for s in cands:
            try:
                df = yf.download(s, period="1d", interval="5m",
                                 progress=False, auto_adjust=False)
            except Exception:
                df = None
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
