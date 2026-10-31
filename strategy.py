# -*- coding: utf-8 -*-
"""
佛系當沖法 — 策略引擎（依《亞當理論》進階版逐字稿）
=====================================================

四階段狀態機（對應逐字稿）
  1) 盤前歸零：觀察清單由選股資料產生（不預設方向、不看新聞）
  2) 盤中觀察：開盤後盯 5 分K，等長黑K（WAIT_TRIGGER）
  3) 進場：長黑K出現 → 下一根 5 分K 開盤立刻放空（TRIGGERED → IN_POSITION）
  4) 出場：移動停損跟隨長黑K開盤價只下移；13:20 時間出場；出場即止不反手（DONE）

【長黑K定義】（逐字稿定義段被截斷，依 2426 鼎元案例反推，參數皆可調）
  a. 黑K：close < open
  b. 長：實體/開盤價 ≥ min_body_pct(0.5%)，且 實體 ≥ body_mult(1.5)×前 body_window(20) 根平均實體
     （盤初歷史 < min_history(5) 根時，僅檢查 a 的門檻）
  c. 空方表態：實體佔全K振幅 ≥ body_range_ratio(0.6)（收在低點附近、影線短）
  案例驗證：8/18 開盤第一根（開 73.2 / 收 72.1，實體 1.50%）→ 觸發 ✓

【移動停損定義】（依逐字稿）
  初始停損 ＝ 觸發長黑K的「開盤價」；
  持倉中每出現一根新長黑K → 停損更新為該根開盤價，且只往下移（跟隨最新證據）；
  股價向上突破停損 → 立刻買回（跳空則以開盤價成交）；13:20 未觸發 → 強制出場；
  出場後不再進場（寧可錯過，不要做錯）。
  實作細節：同一根K不自我否定——長黑K更新停損的那根K，不檢查停損觸發。

【原因碼（供 journal 學習）】
  進場 E_LTBLACK_FIRST（開盤首根長黑K）/ E_LTBLACK_INTRADAY（盤中長黑K）
       E_ENTRY_SHORT（下一根開盤放空）/ E_RULE_BLOCK（核准規則擋下進場）
  停損 S_STOP_UPDATE（新長黑K → 停損下移）
  出場 X_STOP_TOUCH（觸價）/ X_STOP_GAP（跳空穿越）/ X_TIME_1320（時間出場）/ X_EOD（資料收尾）
  標籤 R_TREND_RUN（趨勢延續）/ R_TRAIL_LOCK（移動停損鎖利）/ R_GAP_AWAY（跳空順向）
       R_STOP_REVERSAL（假跌破反轉＝坑）/ R_FLAT_CHOP（盤整耗時）
"""
import json
from datetime import datetime

# ---------------- 預設參數（逐字稿規則 + 可調閾值） ----------------
DEFAULTS = {
    "body_mult": 1.5,        # 實體 ≥ 1.5 × 前N根平均實體
    "body_window": 20,       # N：平均實體回看根數
    "min_body_pct": 0.5,     # 實體/開盤價 ≥ 0.5%
    "body_range_ratio": 0.6, # 實體佔全K振幅 ≥ 60%
    "min_history": 5,        # 平均實體歷史不足時，僅用 min_body_pct
    "entry_cutoff": "13:00", # 此時後不再觸發進場
    "force_exit": "13:20",   # 時間強制出場
    "lots": 1,               # 固定張數（risk_budget > 0 時自動計算）
    "max_lots": 10,          # 張數上限（跳空進場使單張風險極小時，避免張數爆衝）
    "risk_budget": 0.0,      # 單筆最大虧損預算（元）→ 張數 = 預算 ÷ 單張風險
    "max_lot_risk": 0.0,     # 單張風險上限（元）：超過則跳過進場（0＝不設限；直擊高價股大賠根因）
    "min_vol_ratio": 0.0,    # 觸發K最小量能比（0＝不過濾；1.5＝只做放量長黑，實測擋掉 91% 假跌破虧損）
    "entry_mode": "next_open",  # next_open＝長黑K後下一根開盤進場（逐字稿1）／breakout＝等突破觸發K高低點進場（逐字稿2爆量技巧）
    "market_bias_map": None,  # {日期: "up"/"down"} 大盤風向過濾：逆風日跳過進場（None＝不過濾）
    "direction": "short",    # short=長黑K放空（逐字稿原版）／long=長紅K做多（對稱版）
    "eod_exit": True,        # 資料收尾自動出場（回測用）；live 引擎設 False
    "fee_rate": 0.001425,    # 手續費率
    "fee_discount": 0.3,     # 手續費折扣（逐字稿：三折）
    "min_fee": 20.0,         # 單邊手續費下限
    "tax_rate": 0.0015,      # 證交稅（當沖減半，課於賣出腿）
}


def tick_size(p):
    """台股 tick 大小"""
    if p < 10:
        return 0.01
    if p < 50:
        return 0.05
    if p < 100:
        return 0.1
    if p < 500:
        return 0.5
    if p < 1000:
        return 1.0
    return 5.0


def body_of(b):
    return abs(float(b["open"]) - float(b["close"]))


def candle_pattern(bar, prev_raw=None):
    """K 線型態命名：單K解剖型態＋簡易組合（空頭吞沒／連續黑K）"""
    o, h, l, c = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])
    rng = h - l
    if rng <= 0:
        return "一字線"
    body = abs(c - o)
    color = "黑K" if c < o else ("紅K" if c > o else "十字")
    upper = h - max(o, c)
    lower = min(o, c) - l
    if body / rng >= 0.95:
        base = f"{color}光頭光腳（實體佔振幅≥95%）"
    elif body / rng < 0.1:
        base = "十字／紡錘"
    elif lower >= body * 1.5 and lower >= upper:
        base = "長下影線K（假跌破前兆）"
    elif upper >= body * 1.5 and upper >= lower:
        base = "長上影線K（壓回確認）"
    elif c == l:
        base = f"{color}收最低"
    elif c == h:
        base = f"{color}收最高"
    elif upper > lower * 2:
        base = "上影線為主"
    elif lower > upper * 2:
        base = "下影線為主"
    else:
        base = f"普通{color}"
    extra = []
    if prev_raw:
        prev = prev_raw[-1]
        p_o, p_c = float(prev["open"]), float(prev["close"])
        # 空頭吞沒：本根長黑實體完全涵蓋前一根紅K實體
        if c < o and p_c > p_o and o >= p_c and c <= p_o:
            extra.append("空頭吞沒")
        streak = 0
        for pb in reversed(prev_raw):
            if float(pb["close"]) < float(pb["open"]):
                streak += 1
            else:
                break
        if streak + 1 >= 3:
            extra.append(f"連{streak + 1}黑")
    return base + ("＋" + "、".join(extra) if extra else "")


def is_trigger_candle(bar, prev_bodies, cfg, prev_vols=None, prev_raw=None):
    """方向化觸發K：short=長黑K（收<開）、long=長紅K（收>開）；實體門檻兩者相同。
    特徵含 K 棒解剖學（影線/實體佔比/收盤位置）＋ K線型態 ＋ 量能比（當根量÷前N根均量）"""
    o, h, l, c = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])
    if cfg.get("direction", "short") == "long":
        if c <= o:
            return False, {"body_pct": 0.0}
        body = c - o
    else:
        if c >= o:
            return False, {"body_pct": 0.0}
        body = o - c
    feat = {"body": round(body, 3), "body_pct": round(body / o * 100, 3) if o else 0.0}
    rng = h - l
    # K 棒解剖學快照（上影線/下影線/實體佔比/收盤位置）— 供影線與盈虧關聯分析
    feat.update({
        "upper_shadow": round(h - max(o, c), 3),          # 上影線（黑K：h−開）
        "lower_shadow": round(min(o, c) - l, 3),          # 下影線（黑K：收−l）
        "range": round(rng, 3),
        "body_range_ratio": round(body / rng, 3) if rng > 0 else 1.0,  # 實體佔全K振幅
        "upper_sh_ratio": round((h - max(o, c)) / body, 3) if body > 0 else None,
        "lower_sh_ratio": round((min(o, c) - l) / body, 3) if body > 0 else None,
        "close_pos": round((h - c) / (h - l), 3) if rng > 0 else 0.5,  # 1=收在最低（空方完勝）
        "pattern": candle_pattern(bar, prev_raw),
    })
    # 量能比（成交量因素）：當根量 ÷ 前 N 根均量；None＝盤初無歷史量
    vol = float(bar.get("volume", 0) or 0)
    feat["vol"] = vol
    if prev_vols:
        vwin = prev_vols[-cfg.get("body_window", 20):]
        avg_v = sum(vwin) / len(vwin)
        feat["vol_ratio"] = round(vol / avg_v, 3) if avg_v > 0 else None
    else:
        feat["vol_ratio"] = None
    if o > 0 and body / o * 100 < cfg["min_body_pct"]:
        return False, feat
    if rng > 0 and (body / rng) < cfg["body_range_ratio"]:
        return False, feat
    if len(prev_bodies) >= cfg["min_history"]:
        window = prev_bodies[-cfg["body_window"]:]
        avg = sum(window) / len(window)
        feat["prev_avg_body"] = round(avg, 4)
        if body < cfg["body_mult"] * avg:
            return False, feat
    return True, feat


def is_long_black(bar, prev_bodies, cfg, prev_vols=None, prev_raw=None):
    """相容舊名稱：長黑K判定（direction 依 cfg，預設 short）"""
    return is_trigger_candle(bar, prev_bodies, cfg, prev_vols, prev_raw)


# ---------------- 學習用分桶（pit mining 維度） ----------------
def trigger_bucket(hhmm):
    if hhmm <= "09:30":
        return "09:00-09:30"
    if hhmm <= "10:30":
        return "09:30-10:30"
    if hhmm <= "11:30":
        return "10:30-11:30"
    return "11:30-13:00"


def body_pct_band(bp):
    return "<0.7" if bp < 0.7 else ("0.7-1.2" if bp <= 1.2 else ">1.2")


def vol_ratio_band(v):
    """量能比分桶（進場當下即可判定 → 可作為避坑規則維度）
    統計發現：縮量長黑（<1 倍）假跌破率 88%；爆量（≥3 倍）僅 50%"""
    if v is None:
        return "盤初無歷史量"
    if v < 1:
        return "<1 縮量"
    if v < 2:
        return "1~2 倍"
    if v < 3:
        return "2~3 倍"
    return ">=3 倍爆量"


def weekday_of(date_str):
    return ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][
        datetime.strptime(date_str, "%Y-%m-%d").weekday()]


def classify(exit_reason, net):
    """結果標籤：給學習迴路分類每筆交易的「劇本」"""
    if net > 0:
        return {"X_STOP_TOUCH": "R_TRAIL_LOCK", "X_STOP_GAP": "R_GAP_AWAY",
                "X_TIME_1320": "R_TREND_RUN", "X_EOD": "R_TREND_RUN"}.get(exit_reason, "R_TREND_RUN")
    return {"X_STOP_TOUCH": "R_STOP_REVERSAL", "X_STOP_GAP": "R_STOP_REVERSAL",
            "X_TIME_1320": "R_FLAT_CHOP", "X_EOD": "R_FLAT_CHOP"}.get(exit_reason, "R_FLAT_CHOP")


def blocked_by_rules(rules, ctx):
    """核准的避坑規則是否擋下本次進場。
    condition_json 例：{"dimension": "trigger_bucket", "value": ["11:30-13:00"]}
    支援 dimension：trigger_bucket / weekday / symbol / body_pct_band
    """
    for r in rules:
        try:
            cond = json.loads(r["condition_json"])
        except Exception:
            continue
        val = ctx.get(cond.get("dimension", ""))
        if val is not None and val in (cond.get("value") or []):
            return {"name": r["name"], "condition": cond}
    return None


def _close(t, hhmm, fill, reason, cfg, date_str, log=None):
    """出場結算：放空損益 ＋ 手續費（雙腿、三折、有下限）＋ 證交稅（賣出腿、減半）"""
    shares, entry = t["shares"], t["entry_price"]
    gross = (entry - fill) * shares
    sell_val = entry * shares          # 券賣（先賣後買）→ 證交稅在賣出腿
    buy_val = fill * shares
    fee = (max(cfg["min_fee"], sell_val * cfg["fee_rate"] * cfg["fee_discount"])
           + max(cfg["min_fee"], buy_val * cfg["fee_rate"] * cfg["fee_discount"]))
    tax = sell_val * cfg["tax_rate"]
    net = gross - fee - tax
    risk_ps = t["risk_per_share"] or 0
    r = net / (risk_ps * shares) if risk_ps > 0 else 0.0
    t.update({"exit_time": hhmm, "exit_price": fill, "exit_reason": reason,
              "final_stop": t["stop"], "gross_pnl": round(gross, 2),
              "fees": round(fee, 2), "tax": round(tax, 2), "net_pnl": round(net, 2),
              "r_multiple": round(r, 3), "result_tag": classify(reason, net)})
    t["events"].append({"ts": f"{date_str} {hhmm}", "event": "EXIT", "reason": reason,
                        "detail": json.dumps({"price": fill, "net": round(net, 2),
                                              "tag": t["result_tag"],
                                              "stop_trail": [s for _, s in t["stop_updates"]]},
                                             ensure_ascii=False)})
    if log:
        log.info(f"[{date_str}] {t['symbol']} 出場 {fill} @ {hhmm}（{reason}）"
                 f"淨損益 {net:+,.0f} R={r:.2f} 標籤 {t['result_tag']}")


def run_day(day_bars, cfg, symbol, date_str, log=None, rules=None):
    """單日四階段狀態機。回傳 {"trade": dict|None, "signals": [未成交易事件]}"""
    cfg = {**DEFAULTS, **(cfg or {})}
    day_bars = sorted(day_bars, key=lambda x: x["hhmm"])
    signals = []
    prev_bodies, prev_vols, prev_raw = [], [], []

    def push_bar(bar):
        prev_bodies.append(body_of(bar))
        prev_vols.append(float(bar.get("volume", 0) or 0))
        prev_raw.append({k: bar[k] for k in ("open", "high", "low", "close", "volume")})
        if len(prev_raw) > 40:
            del prev_raw[0]
    trigger = trigger_feat = None
    trigger_idx = -1
    pending = False
    t = None

    def add_event(kind, reason, bar, detail):
        t["events"].append({"ts": f"{date_str} {bar['hhmm']}", "event": kind,
                            "reason": reason,
                            "detail": json.dumps(detail, ensure_ascii=False)})

    for i, b in enumerate(day_bars):
        direction = cfg.get("direction", "short")
        # ================= 未進場（第二＋三階段） =================
        if t is None:
            if pending:
                entry = None
                # 進場模式（逐字稿技巧③）：next_open＝下一根開盤；breakout＝等突破觸發K高低點
                if cfg.get("entry_mode", "next_open") == "breakout":
                    trig_l, trig_h = float(trigger["low"]), float(trigger["high"])
                    if direction == "short":
                        if float(b["low"]) <= trig_l:
                            entry = round(min(trig_l, float(b["open"])), 2)
                    else:
                        if float(b["high"]) >= trig_h:
                            entry = round(max(trig_h, float(b["open"])), 2)
                    if entry is None:
                        if b["hhmm"] >= cfg["entry_cutoff"]:  # 截止前未突破 → 放棄觸發
                            pending = False
                        push_bar(b)
                        continue
                else:
                    entry = float(b["open"])
                # 量能過濾：縮量長黑＝假跌破率高（實測 88%）→ 不追空
                vr = (trigger_feat or {}).get("vol_ratio")
                if cfg.get("min_vol_ratio", 0) > 0 and vr is not None \
                        and vr < cfg["min_vol_ratio"]:
                    signals.append({"date": date_str, "symbol": symbol,
                                    "ts": f"{date_str} {b['hhmm']}",
                                    "event": "VOL_SKIP", "reason": "E_VOL_TOO_LOW",
                                    "detail": json.dumps(
                                        {"vol_ratio": vr,
                                         "min_vol_ratio": cfg["min_vol_ratio"],
                                         "trigger_open": trigger["open"]},
                                        ensure_ascii=False)})
                    if log:
                        log.info(f"[{date_str}] 量能比 {vr} < {cfg['min_vol_ratio']} "
                                 f"→ 縮量長黑不追空（E_VOL_TOO_LOW）")
                    pending = False
                    push_bar(b)
                    continue
                init_stop = round(float(trigger["open"]), 2)
                # 倉位管理（逐字稿公式）：單張最大虧損 = |進場價 − 停損價| × 1,000
                per_lot_risk = abs(entry - init_stop) * 1000
                # 單張風險上限（0＝不設限）：直擊「高價股大賠」根因（2454/3008 案例）
                if cfg.get("max_lot_risk", 0) > 0 and per_lot_risk > cfg["max_lot_risk"]:
                    signals.append({"date": date_str, "symbol": symbol,
                                    "ts": f"{date_str} {b['hhmm']}",
                                    "event": "RISK_SKIP", "reason": "E_RISK_TOO_HIGH",
                                    "detail": json.dumps(
                                        {"per_lot_risk": per_lot_risk,
                                         "max_lot_risk": cfg["max_lot_risk"],
                                         "trigger_open": trigger["open"]},
                                        ensure_ascii=False)})
                    if log:
                        log.info(f"[{date_str}] 單張風險 {per_lot_risk:,.0f} 超過上限 "
                                 f"{cfg['max_lot_risk']:,.0f} → 跳過進場（E_RISK_TOO_HIGH）")
                    pending = False
                    push_bar(b)
                    continue
                if cfg["risk_budget"] > 0 and per_lot_risk > 0:
                    # 逐字稿原話：連一張虧損都超出承受 → 不進場
                    if per_lot_risk > cfg["risk_budget"]:
                        signals.append({"date": date_str, "symbol": symbol,
                                        "ts": f"{date_str} {b['hhmm']}",
                                        "event": "BUDGET_SKIP", "reason": "E_RISK_BUDGET",
                                        "detail": json.dumps(
                                            {"per_lot_risk": per_lot_risk,
                                             "risk_budget": cfg["risk_budget"]},
                                            ensure_ascii=False)})
                        if log:
                            log.info(f"[{date_str}] 單張風險 {per_lot_risk:,.0f} 超出預算 "
                                     f"{cfg['risk_budget']:,.0f} → 不進場（E_RISK_BUDGET）")
                        pending = False
                        push_bar(b)
                        continue
                    lots = max(1, min(int(cfg["risk_budget"] // per_lot_risk),
                                      int(cfg.get("max_lots", 10))))
                else:
                    lots = max(1, int(cfg.get("lots", 1)))
                feat = dict(trigger_feat)
                feat.update({
                    "trigger_time": trigger["hhmm"],
                    "trigger_bucket": trigger_bucket(trigger["hhmm"]),
                    "weekday": weekday_of(date_str),
                    "direction": direction,
                    "entry_price": entry, "initial_stop": init_stop,
                    "risk_per_share": round(abs(entry - init_stop), 3),
                    "risk_budget": cfg["risk_budget"] or None, "lots": lots,
                })
                entry_reason = "E_LTBLACK_FIRST" if trigger_idx == 0 else "E_LTBLACK_INTRADAY"
                t = {
                    "date": date_str, "symbol": symbol, "direction": direction,
                    "trigger_time": trigger["hhmm"], "trigger_open": init_stop,
                    "entry_time": b["hhmm"], "entry_price": entry,
                    "initial_stop": init_stop, "final_stop": init_stop,
                    "stop_update_count": 0,
                    "exit_time": None, "exit_reason": None, "exit_price": None,
                    "lots": lots, "shares": lots * 1000,
                    "gross_pnl": None, "fees": None, "tax": None, "net_pnl": None,
                    "risk_per_share": feat["risk_per_share"], "r_multiple": None,
                    "bars_held": 1,
                    "entry_reason": entry_reason, "exit_reason": None, "result_tag": None,
                    "features": json.dumps(feat, ensure_ascii=False),
                    "events": [], "stop_updates": [], "stop": init_stop,
                }
                add_event("ENTRY", f"E_ENTRY_{direction.upper()}", b,
                          {"entry": entry, "initial_stop": init_stop, "lots": lots,
                           "direction": direction,
                           "per_lot_risk": per_lot_risk, "entry_reason": entry_reason})
                if log:
                    act = "進場做多" if direction == "long" else "進場放空"
                    log.info(f"[{date_str}] {symbol} {act} {entry} @ {b['hhmm']}｜"
                             f"初始停損 {init_stop}｜{lots} 張（單張風險 {per_lot_risk:,.0f}）")
                # 進場K自身也是觸發K → 停損立即順向更新（逐字稿案例：73.2 → 72.1）
                ok2, _ = is_trigger_candle(b, prev_bodies, cfg, prev_vols, prev_raw)
                better = (float(b["open"]) > t["stop"]) if direction == "long" \
                    else (float(b["open"]) < t["stop"])
                if ok2 and better:
                    t["stop"] = round(float(b["open"]), 2)
                    t["stop_updates"].append((b["hhmm"], t["stop"]))
                    t["stop_update_count"] += 1
                    add_event("STOP_UPDATE", "S_STOP_UPDATE", b, {"new_stop": t["stop"]})
                    if log:
                        log.debug(f"[{date_str}] 停損更新 → {t['stop']}（進場K自身為觸發K）")
                push_bar(b)
                continue  # 進場K不做停損檢查（同一根K不自我否定）

            ok, feat = is_trigger_candle(b, prev_bodies, cfg, prev_vols, prev_raw)
            if ok:
                # 市場風向過濾（逐字稿5大技巧①）：逆風日不進場
                mb = (cfg.get("market_bias_map") or {}).get(date_str)
                if mb and ((direction == "short" and mb == "up")
                           or (direction == "long" and mb == "down")):
                    signals.append({"date": date_str, "symbol": symbol,
                                    "ts": f"{date_str} {b['hhmm']}",
                                    "event": "MARKET_FILTER", "reason": "E_MARKET_FILTER",
                                    "detail": json.dumps({"market_bias": mb}, ensure_ascii=False)})
                    if log:
                        log.info(f"[{date_str}] 大盤逆風（{mb}）→ 跳過進場")
                    prev_bodies.append(body_of(b))
                    continue
                ctx = {"symbol": symbol, "weekday": weekday_of(date_str),
                       "trigger_bucket": trigger_bucket(b["hhmm"]),
                       "body_pct_band": body_pct_band(feat.get("body_pct", 0.0)),
                       "vol_ratio_band": vol_ratio_band(feat.get("vol_ratio")),
                       "pattern": feat.get("pattern", "")}
                blk = blocked_by_rules(rules or [], ctx)
                if blk:  # 學習迴路：核准的避坑規則擋下進場
                    signals.append({"date": date_str, "symbol": symbol,
                                    "ts": f"{date_str} {b['hhmm']}",
                                    "event": "RULE_BLOCK", "reason": "E_RULE_BLOCK",
                                    "detail": json.dumps({"rule": blk["name"], "ctx": ctx},
                                                         ensure_ascii=False)})
                    if log:
                        log.info(f"[{date_str}] 規則擋下進場：{blk['name']}（{ctx}）")
                    push_bar(b)
                    continue
                trigger, trigger_feat, trigger_idx, pending = b, feat, i, True
                if log:
                    kname = "長紅K" if direction == "long" else "長黑K"
                    log.debug(f"[{date_str}] {kname}觸發 @ {b['hhmm']} "
                              f"o={b['open']} c={b['close']} body%={feat.get('body_pct')}")
            push_bar(b)
            continue

        # ================= 持倉中（第四階段：移動停損＋出場） =================
        t["bars_held"] += 1
        if direction == "long":
            hit = float(b["low"]) < t["stop"]            # 股價跌破停損 → 賣出
            gap = float(b["open"]) < t["stop"]
        else:
            hit = float(b["high"]) > t["stop"]           # 股價突破停損 → 買回
            gap = float(b["open"]) > t["stop"]
        if hit:
            if gap:
                fill = round(float(b["open"]), 2)
            elif direction == "long":
                fill = round(t["stop"] - tick_size(t["stop"]), 2)
            else:
                fill = round(t["stop"] + tick_size(t["stop"]), 2)
            _close(t, b["hhmm"], fill, "X_STOP_GAP" if gap else "X_STOP_TOUCH",
                   cfg, date_str, log)
            break
        ok, _f = is_trigger_candle(b, prev_bodies, cfg, prev_vols, prev_raw)
        if ok:  # 新觸發K → 出場點隨最新證據順向更新（空單下移／多單上移）
            new_stop = round(float(b["open"]), 2)
            better = (new_stop > t["stop"]) if direction == "long" else (new_stop < t["stop"])
            if better:
                t["stop"] = new_stop
                t["stop_updates"].append((b["hhmm"], t["stop"]))
                t["stop_update_count"] += 1
                add_event("STOP_UPDATE", "S_STOP_UPDATE", b, {"new_stop": t["stop"]})
                if log:
                    log.debug(f"[{date_str}] 停損更新 → {t['stop']} @ {b['hhmm']}")
        if b["hhmm"] >= cfg["force_exit"]:  # 13:20 時間出場（不論盈虧）
            _close(t, b["hhmm"], float(b["close"]), "X_TIME_1320", cfg, date_str, log)
            break
        push_bar(b)

    # 資料收尾（僅回測：live 引擎傳 eod_exit=False，未平倉時交由引擎繼續監控）
    if t is not None and t["exit_price"] is None and cfg.get("eod_exit", True):
        last = day_bars[-1]
        _close(t, last["hhmm"], float(last["close"]), "X_EOD", cfg, date_str, log)

    if pending and t is None:  # 觸發了但沒有下一根K可進場
        signals.append({"date": date_str, "symbol": symbol,
                        "ts": f"{date_str} {trigger['hhmm']}",
                        "event": "TRIGGER_NO_ENTRY", "reason": "E_NO_NEXT_BAR",
                        "detail": json.dumps({"trigger_open": trigger["open"]},
                                             ensure_ascii=False)})
    return {"trade": t, "signals": signals}
