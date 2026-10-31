# -*- coding: utf-8 -*-
"""
etf_flow.py — 族群輪動訊號引擎（逐字稿技巧④：資金輪動）
=========================================================
資料源：taiwan-etf-tracker 的 latest.json（D 碟，可由環境變數 ETF_TRACKER_JSON 覆寫）
  - today/prev：21 支主動型 ETF 的持股快照（權重變化 = 機構資金流向）
  - inst：176 檔個股三大法人買賣超（外資/投信/自營商）
  - prices：最新價格

輪動邏輯（逐字稿：老大先漲，小弟跟漲；資金先買龍頭再擴散）：
  ETF 昨日加碼 ＋ 三大法人買超 ＋ 價格強勢 → 族群輪動候選
"""
import json
import os

DEFAULT_PATH = r"D:\taiwan-etf-tracker\latest.json"


def load(path=None):
    p = path or os.environ.get("ETF_TRACKER_JSON") or DEFAULT_PATH
    if not os.path.exists(p):
        raise FileNotFoundError(f"ETF 追蹤資料不存在：{p}")
    return json.load(open(p, encoding="utf-8"))


def etf_flow(d):
    """主動型 ETF 昨日權重變化彙總（today vs prev holdings）
    回傳 (adds, drops)：加碼/減碼排行 [(code, {"add": 權重%, "etfs": [(ETF名, 變化)]})]"""
    today_h, prev_h = d.get("today", {}), d.get("prev", {})
    flow = {}
    for etf, th in today_h.items():
        etf_name = th.get("name", etf)
        ph = prev_h.get(etf, {})
        tmap = {h.get("code"): h.get("weight", 0) for h in th.get("holdings", [])}
        pmap = {h.get("code"): h.get("weight", 0) for h in ph.get("holdings", [])}
        for code, w in tmap.items():
            pw = pmap.get(code)
            if pw is None:
                pw = w  # 新上榜持份不計變化（保守）
            dw = round(w - pw, 3)
            if dw == 0:
                continue
            rec = flow.setdefault(code, {"add": 0.0, "etfs": []})
            rec["add"] += dw
            rec["etfs"].append((etf_name, dw))
    adds = sorted(((c, r) for c, r in flow.items() if r["add"] > 0),
                  key=lambda x: -x[1]["add"])
    drops = sorted(((c, r) for c, r in flow.items() if r["add"] < 0),
                   key=lambda x: x[1]["add"])
    return adds, drops


def inst_flow(d, top_n=15):
    """三大法人買賣超排行（總計）"""
    rows = []
    for code, v in (d.get("inst") or {}).items():
        rows.append((v.get("total", 0), code, v))
    rows.sort(reverse=True)
    return rows[:top_n]


def rotation_candidates(d, top_n=15):
    """族群輪動候選：ETF 加碼 ＋ 三大法人買超 ＋ 價格強勢（可取得的漲跌）
    評分 = ETF加碼權重%（主）＋ 法人買超張數標準化（輔）"""
    adds, _drops = etf_flow(d)
    inst = d.get("inst") or {}
    prices = d.get("prices") or {}
    cands = []
    for code, rec in adds:
        iv = inst.get(code, {}).get("total", 0)
        price = prices.get(code)
        # 評分：ETF 加碼權重為主，法人買超為輔（張 → 0.01 權重當量）
        score = rec["add"] + (iv / 10000 if iv else 0)
        cands.append({"code": code, "add": rec["add"], "inst_total": iv,
                      "price": price, "score": round(score, 3),
                      "etfs": rec["etfs"][:3]})
    cands.sort(key=lambda x: -x["score"])
    return cands[:top_n]
