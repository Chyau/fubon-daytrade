# -*- coding: utf-8 -*-
"""內外盤（逐筆成交）訊號 — 觀察模式

原理（台股慣用定義）：
  外盤（買方主動）：成交價 >= 當時最佳賣價（ask）
  內盤（賣方主動）：成交價 <= 當時最佳買價（bid）
  缺報價時退 tick rule：價較前筆漲→外盤、跌→內盤、平盤→沿用前筆方向

三類訊號（滑動視窗統計，回傳 kind + 明細；僅供觀察，不自動下單）：
  TREND_LONG    順勢偏多：外盤占比 >= buy_hi 且 價漲 >= px_chg
  TREND_SHORT   順勢偏空：內盤占比 >= sell_hi 且 價跌 <= -px_chg
  DIVERGE_SELL  疑似出貨：外盤占比高但價「跌」（買盤吃貨價卻往下 → 拉高不了）
  DIVERGE_BUY   疑似承接：內盤占比高但價「漲」（賣壓沉重價卻撐住 → 有人收）
  （占比不到門檻 → None）

資料來源：REST intraday.trades（fugle_marketdata RestClient.stock，與富邦 SDK
同源檔案；歷史 tick 不在 REST API → 僅能盤中即時）。需要 API Key：
  .env 加 FUBON_API_KEY / FUBON_API_SECRET 後由 live.py 自動啟用。
回應形狀「尚未實測」（無 Key）→ fetch_ticks_rest() 採防禦式多形狀解析。

用法：
    import orderflow
    tr = orderflow.OrderFlowTracker()
    tr.feed(ticks)                    # ticks: [{"price","volume","bid","ask"}, ...]
    sig = tr.signal()                 # None 或 {"kind": "TREND_SHORT", "detail": {...}}

自測（四情境）：
    python orderflow.py
"""
import os
import time
from collections import deque

# ---- 預設門檻（可由 env 覆寫）----
WIN_TICKS = int(os.environ.get("OF_WIN_TICKS", "300"))    # 滑動視窗筆數
MIN_TICKS = int(os.environ.get("OF_MIN_TICKS", "80"))     # 樣本太少不判讀
BUY_HI = float(os.environ.get("OF_BUY_HI", "0.60"))       # 外盤占比門檻
SELL_HI = float(os.environ.get("OF_SELL_HI", "0.60"))     # 內盤占比門檻
PX_CHG = float(os.environ.get("OF_PX_CHG", "0.0015"))     # 價格變化門檻（0.15%）


def classify_tick(price, bid, ask, prev_price=None):
    """回傳 +1（外盤/買方主動）、-1（內盤/賣方主動）、0（無法判定）"""
    try:
        p = float(price)
    except (TypeError, ValueError):
        return 0
    if bid is not None and ask is not None:
        try:
            b, a = float(bid), float(ask)
            if b > 0 and a > 0:
                if p >= a:
                    return 1
                if p <= b:
                    return -1
        except (TypeError, ValueError):
            pass
    # tick rule 後備
    if prev_price:
        try:
            if p > float(prev_price):
                return 1
            if p < float(prev_price):
                return -1
            return 0
        except (TypeError, ValueError):
            return 0
    return 0


class OrderFlowTracker:
    """單一標的的逐筆內外盤統計（滑動視窗）"""

    def __init__(self, win_ticks=WIN_TICKS, min_ticks=MIN_TICKS,
                 buy_hi=BUY_HI, sell_hi=SELL_HI, px_chg=PX_CHG):
        self.win_ticks = max(30, int(win_ticks))
        self.min_ticks = int(min_ticks)
        self.buy_hi, self.sell_hi, self.px_chg = buy_hi, sell_hi, px_chg
        self._buf = deque(maxlen=self.win_ticks)  # (side, volume, price)
        self._prev_price = None

    def feed_one(self, price, volume, bid=None, ask=None):
        side = classify_tick(price, bid, ask, self._prev_price)
        if side == 0 and self._buf:
            side = self._buf[-1][0]      # 平盤→沿用前筆方向（台股慣例）
        try:
            vol = max(0, int(volume or 0))
        except (TypeError, ValueError):
            vol = 0
        p = None
        try:
            p = float(price)
        except (TypeError, ValueError):
            pass
        if p is None or (p <= 0):
            return side
        self._buf.append((side, vol, p))
        self._prev_price = p
        return side

    def feed(self, ticks):
        """批次餵入；ticks 元素可為 dict（price/volume/bid/ask）或 4-tuple"""
        for t in ticks or []:
            if isinstance(t, dict):
                self.feed_one(t.get("price"), t.get("volume"),
                              t.get("bid"), t.get("ask"))
            else:
                self.feed_one(t[0], t[1], t[2] if len(t) > 2 else None,
                              t[3] if len(t) > 3 else None)

    def stats(self):
        n = len(self._buf)
        buy_vol = sell_vol = total = 0
        px_first = px_last = None
        for side, vol, p in self._buf:
            total += vol
            if side >= 0:
                buy_vol += vol
            if side <= 0:
                sell_vol += vol
            if px_first is None:
                px_first = p
            px_last = p
        buy_ratio = (buy_vol / total) if total else 0.0
        sell_ratio = (sell_vol / total) if total else 0.0
        chg = ((px_last / px_first) - 1.0) if (px_first and px_last) else 0.0
        return {"n": n, "total_vol": total, "buy_vol": buy_vol,
                "sell_vol": sell_vol, "buy_ratio": round(buy_ratio, 4),
                "sell_ratio": round(sell_ratio, 4),
                "px_first": px_first, "px_last": px_last,
                "px_chg_pct": round(chg, 5)}

    def signal(self):
        """回傳訊號 dict 或 None（樣本不足、占比不到門檻）"""
        st = self.stats()
        if st["n"] < self.min_ticks or st["total_vol"] <= 0:
            return None
        br, sr, chg = st["buy_ratio"], st["sell_ratio"], st["px_chg_pct"]
        det = {"buy_ratio": f"{br:.0%}", "sell_ratio": f"{sr:.0%}",
               "px_chg_pct": f"{chg * 100:+.2f}%", "px": st["px_last"],
               "n": st["n"], "vol": st["total_vol"]}
        if br >= self.buy_hi and chg >= self.px_chg:
            return {"kind": "TREND_LONG", "detail": det}
        if sr >= self.sell_hi and chg <= -self.px_chg:
            return {"kind": "TREND_SHORT", "detail": det}
        if br >= self.buy_hi and chg <= -self.px_chg:
            return {"kind": "DIVERGE_SELL", "detail": det}   # 外盤多但價跌：疑似出貨
        if sr >= self.sell_hi and chg >= self.px_chg:
            return {"kind": "DIVERGE_BUY", "detail": det}    # 內盤多但價漲：疑似承接
        return None


# ==================== REST 逐筆抓取（防禦式；需 API Key 實測） ====================

def has_api_key():
    return bool(os.environ.get("FUBON_API_KEY"))


def fetch_ticks_rest(rest_client, symbol, limit=500):
    """呼叫 intraday trades 取得最近逐筆成交。

    fugle REST 形狀（文件；未實測，防禦式相容兩種）：
      A: {"date":..., "type":"EQUITY", "symbol":"2330",
          "data":[{"tickTime":..,"bid":..,"ask":..,"price":..,"volume":..}, ...]}
      B: {"data": {"tick": [ ...同上... ]}}
    回傳 [{"price","volume","bid","ask"}, ...]（舊→新或保持原序皆可，tracker 不依賴序）
    """
    out = []
    try:
        res = rest_client.stock.intraday.trades(symbol=str(symbol), limit=int(limit))
    except Exception:
        return out
    items = None
    if isinstance(res, dict):
        data = res.get("data")
        if isinstance(data, dict):            # 形狀 B
            items = data.get("tick")
        elif isinstance(data, list):          # 形狀 A
            items = data
        elif isinstance(res.get("tick"), list):
            items = res.get("tick")
    elif isinstance(res, list):
        items = res
    if not items:
        return out
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            out.append({"price": it.get("price") or it.get("p"),
                        "volume": it.get("volume") or it.get("v") or it.get("qty"),
                        "bid": it.get("bid") or it.get("bidPrice"),
                        "ask": it.get("ask") or it.get("askPrice")})
        except Exception:
            continue
    return out


def build_rest_client():
    """有 API Key 時建立 REST client（fugle_marketdata 套件；失敗回 None）"""
    if not has_api_key():
        return None
    try:
        from fugle_marketdata import RestClient
        return RestClient(api_key=os.environ["FUBON_API_KEY"])
    except Exception:
        try:  # 富邦 SDK 內建同源模組（不同包名）
            from FugleMarketData import RestClient
            return RestClient(api_key=os.environ["FUBON_API_KEY"])
        except Exception:
            return None


# ==================== 合成自測 ====================

def _mk_ticks(price0, step, n, dominant, base=1000):
    """產生合成逐筆：dominant=1 主動買多、-1 主動賣多；價沿 step 變化"""
    ticks, p = [], price0
    for i in range(n):
        vol = base + (i % 7) * 30
        if dominant >= 0:
            bid, ask = round(p - 0.5, 2), round(p + 0.5, 2)
            px = p if (i % 5 == 4) else ask     # 多數打外盤
        else:
            bid, ask = round(p - 0.5, 2), round(p + 0.5, 2)
            px = p if (i % 5 == 4) else bid     # 多數打內盤
        ticks.append({"price": px, "volume": vol, "bid": bid, "ask": ask})
        p += step
        if p <= 1:
            p = 1.0
    return ticks


def selftest(log=print):
    ok = True
    tr = OrderFlowTracker(min_ticks=50)

    # 1) 順勢偏空：內盤多 + 價跌
    tr = OrderFlowTracker(min_ticks=50)
    tr.feed(_mk_ticks(100.0, -0.02, 120, dominant=-1))
    sig = tr.signal()
    log(f"1 順勢空  → {sig['kind'] if sig else 'None'}")
    ok &= bool(sig and sig["kind"] == "TREND_SHORT")

    # 2) 順勢偏多：外盤多 + 價漲
    tr = OrderFlowTracker(min_ticks=50)
    tr.feed(_mk_ticks(100.0, +0.02, 120, dominant=1))
    sig = tr.signal()
    log(f"2 順勢多  → {sig['kind'] if sig else 'None'}")
    ok &= bool(sig and sig["kind"] == "TREND_LONG")

    # 3) 疑似出貨：外盤多但價跌（背離）
    tr = OrderFlowTracker(min_ticks=50)
    tr.feed(_mk_ticks(100.0, -0.02, 120, dominant=1))
    sig = tr.signal()
    log(f"3 背離空  → {sig['kind'] if sig else 'None'}")
    ok &= bool(sig and sig["kind"] == "DIVERGE_SELL")

    # 4) 疑似承接：內盤多但價漲（背離）
    tr = OrderFlowTracker(min_ticks=50)
    tr.feed(_mk_ticks(100.0, +0.02, 120, dominant=-1))
    sig = tr.signal()
    log(f"4 背離多  → {sig['kind'] if sig else 'None'}")
    ok &= bool(sig and sig["kind"] == "DIVERGE_BUY")

    # 5) 樣本不足 → None
    tr = OrderFlowTracker(min_ticks=500)
    tr.feed(_mk_ticks(100.0, 0.0, 60, dominant=1))
    sig = tr.signal()
    log(f"5 樣本不足→ {sig}")
    ok &= sig is None

    # 6) tick rule（無 bid/ask）
    tr = OrderFlowTracker(min_ticks=50)
    ticks = _mk_ticks(100.0, +0.02, 120, dominant=1)
    for t in ticks:
        t["bid"] = t["ask"] = None
    tr.feed(ticks)
    sig = tr.signal()
    log(f"6 無報價  → {sig['kind'] if sig else 'None'}（tick rule）")
    ok &= bool(sig and sig["kind"] == "TREND_LONG")

    log(f"\n自測結果：{'全部通過 ✓' if ok else '有失敗 ✗'}")
    return ok


if __name__ == "__main__":
    import io
    import sys
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass
    selftest()
