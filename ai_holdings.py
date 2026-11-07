# -*- coding: utf-8 -*-
"""AI 持倉建議 — 查庫存 → 判斷適合交易型態（當沖/隔日沖/定期定額/零股存股）
→ 下個交易日賣出/加碼決策 → 依零股/整張執行

每檔庫存輸出：
  style        適合交易型態 ∈ 當沖/隔日沖/定期定額/零股存股（依波動/價位/趨勢/門檻）
  style_reason 型態建議理由
  action       賣出全數/賣出部分/加碼/持有
  reason       決策理由（引用具體數據）

執行：dry 預設；real 需 PLACE_ORDER=true＋儀表板勾選；每標的每日限一次。
安全原則：AI 只產建議，下單經人類逐筆確認；無明確證據 → 持有。
"""
import json
import os
from datetime import datetime

sys_dir = os.path.dirname(os.path.abspath(__file__))
import sys as _sys
if sys_dir not in _sys.path:
    _sys.path.insert(0, sys_dir)

import db as tdb
import llm
import notify
from logutil import get_logger, ensure_utf8_stdout

ensure_utf8_stdout()
log = get_logger("ai_holdings")



# ---------------- ① 資料蒐集 ----------------

def collect(sdk, acc):
    """每檔庫存的決策素材 → (holdings, bank, error)"""
    import accounting as acct
    import fubon_orders as fo
    import positions as pos_mod
    import strategy

    inv = acct.inventories_with_buyinfo(sdk, acc)
    unrl = {u["symbol"]: u for u in acct.unrealized(sdk, acc)}
    bank = acct.bank_remain(sdk, acc)

    conn = tdb.connect()
    conn.row_factory = tdb.sqlite3.Row
    try:
        dt_hist = {}
        for r in conn.execute(
                "SELECT symbol, date, net_pnl FROM trades "
                "WHERE COALESCE(is_dup,0)=0 AND run_id NOT LIKE 'DEMO%' "
                "ORDER BY id DESC LIMIT 300").fetchall():
            dt_hist.setdefault(r["symbol"], []).append(
                {"date": r["date"], "net_pnl": r["net_pnl"]})
    finally:
        conn.close()

    holdings = []
    for it in inv:
        if "Stock" not in (it.get("order_type") or ""):
            continue   # AI 決策只看現股庫存
        sym = it["symbol"]
        qty_total = it.get("qty_total") or 0
        if qty_total <= 0:
            continue
        u = unrl.get(sym) or {}
        closes, prev_chg, amplitude = [], None, None
        try:
            bars = fo.fetch_fubon_history(sym, days_back=14, timeframe="5") or []
            dc, day_low, day_high = {}, {}, {}
            for b in bars:
                dc[b["date"]] = b["close"]
                day_low[b["date"]] = min(day_low.get(b["date"], 9e9), float(b["low"]))
                day_high[b["date"]] = max(day_high.get(b["date"], 0), float(b["high"]))
            days = sorted(dc)[-11:]
            closes = [{"date": d, "close": dc[d]} for d in days]
            if len(days) >= 2:
                prev_chg = round((dc[days[-1]] / dc[days[-2]] - 1) * 100, 2)
            if len(days) >= 5:
                amps = [(day_high[d] - day_low[d]) / dc[d] * 100
                        for d in days if dc[d]]
                amplitude = round(sum(amps) / len(amps), 2)   # 日均振幅%
        except BaseException as e:
            log.debug(f"[holdings][{sym}] 收盤序列失敗：{str(e)[:80]}")
        t60 = None
        try:
            r60 = fo.trend_60k(sdk, sym)
            t60 = r60.get("trend") if r60 else None
        except BaseException:
            pass
        px = pos_mod.last_price(sdk, sym)
        vwap_pos = None
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            bt = [b for b in (fo.fetch_fubon_history(sym, days_back=1,
                                                     timeframe="5") or [])
                  if b["date"] == today]
            vwap = fo.vwap_of_day(bt)
            vwap_pos = strategy.vwap_pos(vwap, px)
        except BaseException:
            pass
        holdings.append({
            "symbol": sym,
            "qty_whole": it.get("qty_whole") or 0,
            "qty_odd": it.get("qty_odd") or 0,
            "qty_total": qty_total,
            "last_buy_date": it.get("last_buy_date"),
            "cost_price": u.get("cost_price") or it.get("buy_avg"),
            "unrealized_net": u.get("net"),
            "last_price": px,
            "prev_chg": prev_chg,
            "amplitude": amplitude,
            "t60": t60,
            "vwap_pos": vwap_pos,
            "recent_closes": closes,
            "recent_daytrades": (dt_hist.get(sym) or [])[:5],
        })
    return holdings, bank, None


# ---------------- ② LLM 評估 ----------------

HOLDINGS_PROMPT = """你是台股現股持倉的風控顧問。輸入是每檔庫存的完整數據
（整股/零股股數、成本、未實現損益、近10日收盤、日均振幅%、60K趨勢、均價線位置、前日漲幅、同標的當沖歷史）。
請對「每一檔」輸出兩個判斷：

A. style（最適交易型態，四選一）：
- 當沖：日均振幅 ≥2.5%、成交量足、60K 有明確方向 → 適合日內進出
- 隔日沖：趨勢轉折初期（如剛漲停/剛突破、動能強）→ 持有 1~3 日
- 定期定額：大型權值/ETF、低振幅（<1.5%）、長期穩定 → 固定金額累積
- 零股存股：股價高（整張門檻 ≥15萬）或穩定配息型 → 小額累積
B. action（下個交易日動作）：賣出全數/賣出部分/加碼/持有
- 零股（<1000股）只能現股交易（1~999股）；整張數量須為 1000 倍數
- 虧損且跌破近10日低點/趨勢轉空（60K空頭＋均價下）→ 賣出全數
- 無明確證據 → 持有（保守優先）
- 理由 ≤60 字，引用具體數據

輸出 JSON：
{"decisions": [{"symbol": "...", "style": "當沖|隔日沖|定期定額|零股存股",
"style_reason": "≤40字", "action": "...", "qty": 股數或null,
"limit_price": 數字或null, "reason": "..."}]}"""


def evaluate(sdk, acc):
    """回傳 (decisions list, error)"""
    if not llm.enabled():
        return None, "LLM 未設定（.env：LLM_BASE_URL/LLM_API_KEY/LLM_MODEL）"
    holdings, bank, err = collect(sdk, acc)
    if err:
        return None, err
    if not holdings:
        return None, "目前無庫存"
    user = json.dumps({"holdings": holdings, "bank": bank},
                      ensure_ascii=False, default=str)
    text, lerr = llm.chat(
        [{"role": "system", "content": HOLDINGS_PROMPT},
         {"role": "user", "content": user}],
        json_mode=True, max_tokens=1200)
    if not text:
        return None, lerr or "LLM 無回應"
    try:
        data = json.loads(text)
        decisions = data.get("decisions", [])
    except Exception:
        return None, f"AI 輸出非 JSON：{text[:300]}"
    valid = {h["symbol"] for h in holdings}
    decisions = [d for d in decisions if d.get("symbol") in valid]
    save_review(json.dumps(decisions, ensure_ascii=False, indent=1))
    return decisions, None


def save_review(content):
    conn = tdb.connect()
    try:
        conn.execute("INSERT INTO ai_reviews(created_at, date, kind, content) "
                     "VALUES (datetime('now','localtime'), ?, 'holdings', ?)",
                     (datetime.now().strftime("%Y-%m-%d"), content))
        conn.commit()
    finally:
        conn.close()


# ---------------- ③ 執行 ----------------

def execute(sdk, acc, decision, real=False):
    """依決策下單（dry 預設；real 需 PLACE_ORDER=true）。每標的每日限一次。"""
    import accounting as acct
    sym = decision.get("symbol")
    action = decision.get("action", "")
    if not sym or not action:
        return {"ok": False, "message": "決策缺 symbol/action"}
    conn = tdb.connect()
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM orders WHERE mode='ai' AND ts LIKE ? "
            "AND symbol=? AND is_success=1 AND message NOT LIKE '%dry%'",
            (f"{datetime.now().strftime('%Y-%m-%d')}%", sym)).fetchone()[0]
    finally:
        conn.close()
    if n >= 1:
        return {"ok": False, "message": f"{sym} 今日已執行過 AI 建議（每日一次）"}

    # 只認現股（Stock）列 — 融券/SBL/融資列是他人部位或另類持倉，不可混賣
    inv = {}
    for it0 in acct.inventories(sdk, acc):
        if "Stock" not in (it0.get("order_type") or ""):
            continue
        cur = inv.get(it0["symbol"])
        if cur:
            cur["qty_whole"] += it0.get("qty_whole") or 0
            cur["qty_odd"] += it0.get("qty_odd") or 0
            cur["qty_total"] += it0.get("qty_total") or 0
        else:
            inv[it0["symbol"]] = dict(it0)
    it = inv.get(sym)
    if not it or (it.get("qty_total") or 0) <= 0:
        return {"ok": False, "message": f"{sym} 目前無庫存"}

    import positions as pos_mod
    px = pos_mod.last_price(sdk, sym) or 0
    dry = not real
    result = {"ok": False, "message": "", "order_no": None}

    if action == "賣出全數":
        odd_qty = it.get("qty_odd") or 0
        whole = (it.get("qty_whole") or 0) // 1000 * 1000
        result = {"ok": True, "message": "無可賣數量", "order_no": None}
        if odd_qty:
            r = acct.place_manual(sdk, acc, sym, "sell", odd_qty,
                                  price=_odd_limit(px, "sell"), odd=True,
                                  dry=dry, user_def="AIREV", mode_tag="ai")
            result = {"ok": r["ok"], "message": f"賣出零股 {odd_qty}｜{r['message']}",
                      "order_no": r.get("order_no")}
        if whole >= 1000:
            r2 = acct.place_manual(sdk, acc, sym, "sell", whole, price=None,
                                   odd=False, dry=dry, user_def="AIREV",
                                   mode_tag="ai")
            result = {"ok": r2["ok"], "message": f"賣出整張 {whole} 股｜{r2['message']}",
                      "order_no": r2.get("order_no")}
    elif action == "賣出部分":
        qty = int(decision.get("qty") or 0)
        odd_qty = it.get("qty_odd") or 0
        if 1 <= qty <= 999:
            r = acct.place_manual(sdk, acc, sym, "sell", qty,
                                  price=_odd_limit(px, "sell"), odd=True,
                                  dry=dry, user_def="AIREV", mode_tag="ai")
            result = {"ok": r["ok"], "message": f"賣出零股 {qty}｜{r['message']}",
                      "order_no": r.get("order_no")}
        elif qty >= 1000 and qty % 1000 == 0 and qty <= it.get("qty_whole", 0):
            r = acct.place_manual(sdk, acc, sym, "sell", qty, price=None,
                                  odd=False, dry=dry, user_def="AIREV",
                                  mode_tag="ai")
            result = {"ok": r["ok"], "message": f"賣出整張 {qty} 股｜{r['message']}",
                      "order_no": r.get("order_no")}
        else:
            result = {"ok": False, "message": f"賣出數量 {qty} 不合法"}
    elif action == "加碼":
        qty = int(decision.get("qty") or 0)
        lp = decision.get("limit_price")
        if 1 <= qty <= 999:
            r = acct.place_manual(sdk, acc, sym, "buy", qty,
                                  price=lp or _odd_limit(px, "buy"), odd=True,
                                  dry=dry, user_def="AIREV", mode_tag="ai")
            result = {"ok": r["ok"], "message": f"加碼零股 {qty}｜{r['message']}",
                      "order_no": r.get("order_no")}
        elif qty >= 1000 and qty % 1000 == 0:
            r = acct.place_manual(sdk, acc, sym, "buy", qty, price=lp,
                                  odd=False, dry=dry, user_def="AIREV",
                                  mode_tag="ai")
            result = {"ok": r["ok"], "message": f"加碼整張 {qty} 股｜{r['message']}",
                      "order_no": r.get("order_no")}
        else:
            result = {"ok": False, "message": f"加碼數量 {qty} 不合法"}
    else:   # 持有
        result = {"ok": True, "message": f"{sym} 持有（不下單）"}

    if result.get("ok"):
        notify.send(f"【AI 持倉執行】{sym} {action}｜{result['message']}"
                    + ("（dry）" if dry else ""))
    return result


def _odd_limit(px, side):
    """零股積極限價：買=市價+1tick、賣=市價−1tick（零股僅限限價單）"""
    try:
        import accounting as acct
        t = acct.tick_size_odd(px)
        if side == "buy":
            return round(px + t, 2)
        return round(max(px - t, t), 2)
    except BaseException:
        return None
