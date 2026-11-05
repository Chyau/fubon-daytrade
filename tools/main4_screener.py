# -*- coding: utf-8 -*-
"""主力四訊號篩選器（來源：逐字稿 2026-11 — 主力吸籌到發動的四大痕跡）

四訊號（全部以日K計算，資料源：權證小哥 tw_stock.db 的 technical_indicator 表）：
  S1 區間整理＋低點不破   ：近20日振幅收斂、近5日不破區間低點、低點墊高 → 賣壓消失
  S2 整理後連續量增       ：近3日均量 ≥ 前17日均量×1.5，且尚未大漲（20日漲幅≤15%）
  S3 突破後回調不回區間   ：近5日曾收盤突破區間上緣，突破後最低點不跌回上緣 → 籌碼轉移
  S4 發動確認（加分）     ：跳空≥2% 或 近3日有單日漲幅≥9%（缺口/漲停 = 主力不計成本買）

判定：
  score=3（S1+S2+S3）→ 「發動候選」：隔日盯盤做多（B1/next_open 多單）
  score=2（S1+S2）   → 「吸籌監視」：列入觀察，等 S3
  只有 S4            → 警語（大漲後的量增意義完全不同，不做）

⚠ 2026-11-05 歷史事件研究（50 檔 × 347 日，隔日開盤進場，對照全體基準）：
  四訊號齊備（s123, n=245）：+5日 +0.58% vs 基準 +2.07% → 「無」超額報酬，勿迷信
  唯一有 edge 的是 S4，且全來自「漲停」組：
    漲停(收盤漲幅≥9%) 次日當沖：n=114 勝率 84% 平均 +4.74%（開→高 86% +5.01%）
    跳空2~5% 次日當沖：n=378 勝率 38% 平均 -1.25%（開高回吐，fade gap）
    跳空>5%  次日當沖：n=72  勝率 22% 平均 -2.91%
  → 可行方向＝「漲停次日動能做多」（limitup_next），跳空組反而該迴避或反做。

用法：
  python tools/main4_screener.py scan        ＃ 基準日掃描，輸出候選清單＋寫 data/main4_screener.json
  python tools/main4_screener.py study       ＃ 四訊號事件研究（隔日開盤進場 1/2/5 日）
  python tools/main4_screener.py study-limitup ＃ 漲停次日動能模擬（開盤進、2%停損、收盤出）
                                              ＃ 含可成交性檢查：次日開盤已近漲停（≥9.5%）→ 跳過
鐵律：先 study 有正期望證據，才考慮掛進 app 建議清單（觀察→驗證→實戰）。
"""
import argparse
import io
import json
import os
import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from logutil import get_logger, ensure_utf8_stdout

ensure_utf8_stdout()
log = get_logger("main4", "INFO")

TWDB = r"D:\權證小哥-當沖飆股神手\twdata\tw_stock.db"
OUT_JSON = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "main4_screener.json")

# ===== 可調參數（保守初值；驗證後才調） =====
P = dict(
    win=20,                 # 區間窗
    base_n=15,              # 吸籌基準窗（前 15 日）
    max_range_pct=0.18,     # S1：區間振幅上限（18%）
    hl_tol=0.995,           # S1：低點墊高容許（後10日最低 ≥ 前10日最低×0.995）
    vol_mult=1.5,           # S2：量增倍數（近3日均量 vs 前17日均量）
    vol_days=3,             # S2：連續量增天數（逐字稿：不要只看一天）
    max_runup=0.15,         # S2：防「漲後量增」— 基準日距均價漲幅上限
    brk_days=5,             # S3：突破觀察窗
    brk_tol=0.99,           # S3：回調不回區間容許（≥上緣×0.99）
    gap_pct=0.02,           # S4：跳空門檻
    limit_pct=0.09,         # S4：漲停門檻（台股 10% 附近）
)


def load_daily(db_path=TWDB):
    """回傳 {stock_no: [bars]}，bars 依日期升序；bar = (date, open, high, low, close, volume)"""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT stock_no, date, open_price, high_price, low_price, close_price, volume "
        "FROM technical_indicator ORDER BY stock_no, date").fetchall()
    con.close()
    data = defaultdict(list)
    for r in rows:
        o, h, l, c, v = (r["open_price"], r["high_price"], r["low_price"],
                         r["close_price"], r["volume"])
        if not (o and h and l and c and v):
            continue
        data[r["stock_no"]].append({
            "date": r["date"], "o": float(o), "h": float(h),
            "l": float(l), "c": float(c), "v": float(v)})
    return dict(data)


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def detect(bars, i, p=P):
    """對 bars[:i+1]（含 i 當日）判定四訊號。i 需 ≥ win。
    回傳 dict(s1,s2,s3,s4,score,verdict,detail)"""
    if i + 1 < p["win"]:
        return None
    w = bars[i + 1 - p["win"]: i + 1]
    base, recent5 = w[:p["base_n"]], w[-p["brk_days"]:]
    rng_hi = max(b["h"] for b in base)
    rng_lo = min(b["l"] for b in base)
    avg_c = _mean([b["c"] for b in base])
    c0 = w[-1]["c"]
    if avg_c <= 0 or rng_lo <= 0:
        return None
    detail = {}

    # S1：低點不破（賣壓消失）
    range_pct = (rng_hi - rng_lo) / avg_c
    no_new_low = min(b["l"] for b in recent5) >= rng_lo
    hl_ok = (min(b["l"] for b in w[10:]) >=
             min(b["l"] for b in w[:10]) * p["hl_tol"])
    s1 = no_new_low and hl_ok and range_pct <= p["max_range_pct"]
    detail["range_pct"] = round(range_pct * 100, 1)

    # S2：整理後連續量增（非漲後量增）
    vol_r = _mean([b["v"] for b in w[-p["vol_days"]:]])
    vol_b = _mean([b["v"] for b in w[:-p["vol_days"]]])
    s2 = (vol_b > 0 and vol_r >= vol_b * p["vol_mult"]
          and c0 <= avg_c * (1 + p["max_runup"]))
    detail["vol_x"] = round(vol_r / vol_b, 2) if vol_b else None

    # S3：突破後回調不回區間（籌碼轉移）
    brk_idx = next((j for j, b in enumerate(recent5) if b["c"] > rng_hi), None)
    if brk_idx is not None:
        after = recent5[brk_idx:]
        pullback_ok = min(b["l"] for b in after) >= rng_hi * p["brk_tol"]
    else:
        pullback_ok = False
    s3 = brk_idx is not None and pullback_ok

    # S4：跳空或漲停（發動確認，加分）
    prev_c = bars[i - 1]["c"] if i >= 1 else None
    gap_up = prev_c and w[-1]["o"] >= prev_c * (1 + p["gap_pct"])
    near_limit = any(j > 0 and bars[j]["c"] / bars[j - 1]["c"] - 1 >= p["limit_pct"]
                     for j in range(max(1, i - 2), i + 1))
    s4 = bool(gap_up or near_limit)
    detail["s4_kind"] = ("跳空" if gap_up else "") + ("漲停" if near_limit else "")

    score = int(s1) + int(s2) + int(s3)
    verdict = ("發動候選" if score == 3 else
               "吸籌監視" if (s1 and s2) else
               ("僅發動（無吸籃）" if s4 and score == 0 else ""))
    return {"s1": s1, "s2": s2, "s3": s3, "s4": s4, "score": score,
            "verdict": verdict, "detail": detail}


def cmd_scan(db_path=TWDB, save=True):
    data = load_daily(db_path)
    out = {"generated": None, "params": {k: v for k, v in P.items()},
           "candidates": [], "watch": [], "limitup": []}
    import datetime
    out["generated"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    hits = 0
    for sym, bars in sorted(data.items()):
        i = len(bars) - 1
        if i < 1:
            continue
        d = detect(bars, i)
        chg = bars[i]["c"] / bars[i - 1]["c"] - 1 if bars[i - 1]["c"] else 0
        # ⚡漲停清單（實證最有 edge 的方向：limitup_next 次日動能做多）
        if chg >= P["limit_pct"]:
            out["limitup"].append({"stock_no": sym, "date": bars[i]["date"],
                                   "close": bars[i]["c"], "chg_pct": round(chg * 100, 1)})
            log.info(f"{sym} 漲停（+{chg*100:.1f}%）收 {bars[i]['c']} → "
                     f"明日 limitup_next 候選（跳空≤1% 才進、停損跌回漲停價）")
        if not d:
            continue
        if d["score"] >= 3 or (d["s1"] and d["s2"]):
            row = {"stock_no": sym, "date": bars[i]["date"], "close": bars[i]["c"],
                   "verdict": d["verdict"], "s4": d["s4"], "detail": d["detail"]}
            (out["candidates"] if d["score"] >= 3 else out["watch"]).append(row)
            hits += 1
            tag = " ⚡" + d["detail"]["s4_kind"] if d["s4"] else ""
            log.info(f"{sym} {d['verdict']}｜收 {bars[i]['c']}｜振幅 {d['detail']['range_pct']}%"
                     f"｜量 {d['detail']['vol_x']}×{tag}")
    log.info(f"掃描完成：{len(data)} 檔 → 漲停 {len(out['limitup'])}｜"
             f"四訊號命中 {hits}（發動 {len(out['candidates'])}｜監視 {len(out['watch'])}）")
    if save and (hits or out["limitup"]):
        os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
        with open(OUT_JSON, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        log.info(f"已寫入 {OUT_JSON}")
    return out


def cmd_study(db_path=TWDB, fwd=(1, 2, 5)):
    """歷史事件研究：每檔每天算訊號，命中「發動候選」→ 統計隔日開盤進場的 N 日報酬。
    對照基準：同一窗內全部日子的隔日進場報酬（等同無腦每天做多）。"""
    data = load_daily(db_path)
    ev = defaultdict(list)   # fwd_n -> [(ret, ...)] 命中組
    base = defaultdict(list)  # fwd_n -> [(ret, ...)] 全體
    n_events = 0
    for sym, bars in data.items():
        for i in range(len(bars) - 1):
            d = detect(bars, i)
            if d is None:
                continue
            en = bars[i + 1]["o"]     # 隔日開盤進場（與引擎 next_open 同語意）
            for n in fwd:
                if i + n < len(bars):
                    xp = bars[i + n]["c"]
                    base[n].append(xp / en - 1)
            if d["score"] >= 3:
                n_events += 1
                for n in fwd:
                    if i + n < len(bars):
                        ev[n].append(bars[i + n]["c"] / en - 1)
    print(f"事件數（發動候選）：{n_events}")
    for n in fwd:
        e, b = ev[n], base[n]
        if not e:
            print(f"  +{n}日：無樣本")
            continue
        wr = sum(1 for x in e if x > 0) / len(e)
        wr_b = sum(1 for x in b if x > 0) / len(b) if b else 0
        print(f"  +{n}日：n={len(e)}  勝率 {wr:.0%}（基準 {wr_b:.0%}）  "
              f"平均 {sum(e)/len(e):+.2%}（基準 {sum(b)/len(b):+.2%}）")


def cmd_study_limitup(db_path=TWDB, limit_pct=None, stop_pct=None, stop_mode="prev",
                      max_gap=0.01):
    """漲停次日動能模擬（日K近似）。
    訊號：訊號日收盤漲幅 ≥ limit_pct → 次日開盤進場做多
    停損（兩種模式）：
      prev（預設，實證較佳）：跌回昨收（漲停價）＝動能失效（逐字稿：走勢違背預期即出場）
      pct：進場價 × (1-stop_pct)
    出場：停損觸發以日低判定（保守假設停在停損價）；否則收盤出（日K收盤≈13:30）
    過濾：
      可成交性：次日開盤 ≥ 昨收×1.095（開盤即漲停，排隊買不到）→ 跳過
      max_gap（預設 1%）：隔日跳空超過此值不進（實證：跳空越大報酬越差，
        0~1% 組 n=180 勝率57% +6.71%；>5% 組 -1.16%）
    附加統計：連續第幾根漲停、近期（近3個月）edge 衰退檢查"""
    limit_pct = limit_pct if limit_pct is not None else P["limit_pct"]
    data = load_daily(db_path)
    rows = []          # (sym, date, consec, gap, ret)
    n_skip_fill = n_skip_gap = 0
    for sym, bars in data.items():
        consec = 0
        for i in range(1, len(bars) - 1):
            sig = bars[i]["c"] / bars[i - 1]["c"] - 1 >= limit_pct
            consec = consec + 1 if sig else 0
            if not sig:
                continue
            nb = bars[i + 1]
            pc = bars[i]["c"]
            gap = nb["o"] / pc - 1
            if nb["o"] >= pc * 1.095:
                n_skip_fill += 1
                continue
            if gap > max_gap:
                n_skip_gap += 1
                continue
            entry = nb["o"]
            stop = pc if stop_mode == "prev" else round(entry * (1 - stop_pct), 2)
            ret = (stop / entry - 1) if nb["l"] <= stop else (nb["c"] / entry - 1)
            rows.append((sym, nb["date"], consec, gap, ret))
    if not rows:
        print("無樣本")
        return
    rets = [r[4] for r in rows]
    wr = sum(1 for x in rets if x > 0) / len(rets)
    stop_desc = ("跌回昨收（動能失效）" if stop_mode == "prev"
                 else f"進場價-{stop_pct:.1%}")
    print(f"漲停(≥{limit_pct:.0%})次日動能｜停損：{stop_desc}｜跳空上限 {max_gap:.0%}｜"
          f"可成交性跳過 {n_skip_fill}｜跳空超限跳過 {n_skip_gap}")
    print(f"總樣本 n={len(rows)}  勝率 {wr:.0%}  平均 {sum(rets)/len(rets):+.2%}  "
          f"中位數 {sorted(rets)[len(rets)//2]:+.2%}")
    by_c = defaultdict(list)
    for _, _, c, _, r in rows:
        by_c[min(c, 3)].append(r)
    names = {1: "首板漲停", 2: "二連板", 3: "三連板以上"}
    for c in sorted(by_c):
        rs = by_c[c]
        w = sum(1 for x in rs if x > 0) / len(rs)
        print(f"  {names[c]:<8} n={len(rs):<4} 勝率 {w:.0%}  平均 {sum(rs)/len(rs):+.2%}")
    recent = [r[4] for r in rows if r[1] >= "2026/08"]
    if recent:
        w = sum(1 for x in recent if x > 0) / len(recent)
        print(f"  近 3 個月  n={len(recent):<4} 勝率 {w:.0%}  平均 {sum(recent)/len(recent):+.2%}"
              f"（edge 衰退檢查）")


def main():
    ap = argparse.ArgumentParser(description="主力四訊號篩選器")
    ap.add_argument("cmd", choices=["scan", "study", "study-limitup"])
    ap.add_argument("--db", default=TWDB)
    ap.add_argument("--limit-pct", type=float, default=None,
                    help="漲停門檻（預設 0.09）")
    ap.add_argument("--stop-mode", choices=["prev", "pct"], default="prev",
                    help="停損模式：prev=跌回昨收（實證較佳）；pct=固定百分比")
    ap.add_argument("--stop-pct", type=float, default=0.02,
                    help="pct 模式停損幅度")
    ap.add_argument("--max-gap", type=float, default=0.01,
                    help="隔日跳空上限（預設 0.01；實證跳空越大報酬越差）")
    a = ap.parse_args()
    if a.cmd == "scan":
        cmd_scan(a.db)
    elif a.cmd == "study-limitup":
        cmd_study_limitup(a.db, a.limit_pct, a.stop_pct, a.stop_mode, a.max_gap)
    else:
        cmd_study(a.db)


if __name__ == "__main__":
    main()
