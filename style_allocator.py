# -*- coding: utf-8 -*-
"""型態配置引擎 — 把系統全部數據統籌，為每檔標的配對最適交易型態

四種型態（各含評分與證據）：
  當沖     波動足＋流動性足＋60K 方向明確＋該標的當沖歷史勝率（引擎實測）
  隔日沖   漲停次日動能（實證 n=180 勝率 57% avg +6.71%；跳空≤1% 才進）
  定期定額 低振幅＋流動性高＋整張門檻低 → 固定金額累積
  零股存股 整張門檻高（≥15萬）或穩定型 → 零股小額參與

資料源（全部既有）：
  Fubon 5 分K（振幅/量/價）→ 沙箱關閉時退回 tw_stock.db 日K（technical_indicator）
  trades.db 同標的當沖歷史（筆數/勝率/avgR — 引擎實測證據）
  多空清單（bias/KD/MACD/MA 標籤）
  60K 趨勢（有 SDK 連線時）
  前日漲幅（漲停偵測 → 隔日沖）

用法：
  python style_allocator.py                ＃ 全標的評分表
  python style_allocator.py --style 當沖   ＃ 只看某型態的前幾名
輸出：終端表格 ＋ data/style_alloc.json（儀表板 /style 讀取）
"""
import argparse
import io
import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as tdb
from logutil import get_logger, ensure_utf8_stdout

ensure_utf8_stdout()
log = get_logger("style_alloc", "INFO")

TWDB = r"D:\權證小哥-當沖飆股神手\twdata\tw_stock.db"
OUT_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "data", "style_alloc.json")
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "data", "style_alloc_cache.json")


# ---------------- 特性計算 ----------------

def bars_from_twdb(db_path=TWDB):
    """離線後備：tw_stock.db 日K（50 檔）→ {sym: [bars]}"""
    if not os.path.exists(db_path):
        return {}
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT stock_no, date, open_price, high_price, low_price, close_price, volume "
        "FROM technical_indicator ORDER BY stock_no, date").fetchall()
    con.close()
    data = {}
    for r in rows:
        try:
            o, h, l, c, v = (float(r["open_price"]), float(r["high_price"]),
                             float(r["low_price"]), float(r["close_price"]),
                             float(r["volume"] or 0))
        except (TypeError, ValueError):
            continue
        data.setdefault(r["stock_no"], []).append(
            {"date": r["date"].replace("/", "-"), "open": o, "high": h,
             "low": l, "close": c, "volume": v})
    return data


def features_from_bars(bars):
    """由日K/5分K 算特性：振幅/價/量/前日漲幅"""
    if len(bars) < 6:
        return None
    amps = [(b["high"] - b["low"]) / b["close"] * 100
            for b in bars[-10:] if b["close"]]
    amp = sum(amps) / len(amps)
    price = bars[-1]["close"]
    vols = [b["volume"] for b in bars[-10:]]
    avg_vol = sum(vols) / len(vols)
    prev_chg = None
    if len(bars) >= 2 and bars[-2]["close"]:
        prev_chg = round((bars[-1]["close"] / bars[-2]["close"] - 1) * 100, 2)
    closes = [{"date": b["date"], "close": b["close"]} for b in bars[-10:]]
    return {"price": round(price, 2), "amp": round(amp, 2),
            "avg_vol": round(avg_vol, 0), "prev_chg": prev_chg,
            "lot_value": round(price * 1000, 0), "closes": closes}


def dt_stats(conn, sym):
    """同標的當沖歷史（引擎實測證據）→ {n, win_rate, avg_r}"""
    rows = conn.execute(
        "SELECT net_pnl, r_multiple FROM trades WHERE symbol=? "
        "AND COALESCE(is_dup,0)=0 AND run_id NOT LIKE 'DEMO%' "
        "ORDER BY id DESC LIMIT 60", (sym,)).fetchall()
    n = len(rows)
    if n < 3:
        return {"n": 0, "win_rate": None, "avg_r": None}
    wr = sum(1 for r in rows if (r["net_pnl"] or 0) > 0) / n
    ar = sum(r["r_multiple"] or 0 for r in rows) / n
    return {"n": n, "win_rate": round(wr, 2), "avg_r": round(ar, 2)}


def longshort_tags():
    """多空清單（bias/KD/MACD/MA 標籤）→ {sym: {...}}"""
    try:
        import longshort
        date, rows = longshort.load_longshort(
            labels=("做多", "偏多", "觀望", "偏空", "做空"))
        return {r["stock_no"]: {"bias": r["bias_label"],
                                "score": r.get("bias_score"),
                                "tags": r.get("tags", [])} for r in rows}, date
    except BaseException as e:
        log.debug(f"多空清單載入失敗：{e}")
        return {}, None


# ---------------- 四型態評分 ----------------

def score_styles(feat, dt, ls, t60):
    """回傳 {"當沖": (score, [reasons]), ...} ＋ best"""
    amp, price = feat["amp"], feat["price"]
    lot_value = feat["lot_value"]
    vol_lots = feat["avg_vol"] / 1000.0
    prev_chg = feat["prev_chg"]
    n, wr, ar = dt.get("n", 0), dt.get("win_rate"), dt.get("avg_r")
    out = {}

    # ---- 當沖 ----
    s, rs = 0, []
    if amp >= 2.5:
        s += 40; rs.append(f"日均振幅 {amp:.1f}%（≥2.5% 波動足）")
    elif amp >= 2.0:
        s += 20; rs.append(f"振幅 {amp:.1f}%（尚可）")
    else:
        rs.append(f"振幅 {amp:.1f}%（偏低，當沖空間不足）")
    if vol_lots >= 1000:
        s += 25; rs.append(f"日均量 {vol_lots:,.0f} 張（流動性佳）")
    elif vol_lots >= 200:
        s += 10; rs.append(f"日均量 {vol_lots:,.0f} 張")
    else:
        rs.append(f"日均量 {vol_lots:,.0f} 張（偏薄）")
    if t60 in ("up", "down"):
        s += 15; rs.append(f"60K 方向明確（{'多' if t60 == 'up' else '空'}頭）")
    if n >= 5 and wr is not None:
        if wr >= 0.5:
            s += 20; rs.append(f"當沖實測勝率 {wr:.0%}（n={n}，引擎歷史）")
        elif wr < 0.4:
            s -= 10; rs.append(f"當沖實測勝率僅 {wr:.0%}（n={n}）")
        else:
            rs.append(f"當沖實測勝率 {wr:.0%}（n={n}）")
    if amp < 2.0 or vol_lots < 200:
        s = int(s * 0.3)
    out["當沖"] = (max(0, min(100, s)), rs)

    # ---- 隔日沖 ----
    s, rs = 0, []
    if prev_chg is not None and prev_chg >= 9:
        s += 60
        rs.append(f"昨收漲停 {prev_chg:+.1f}%（實證次日動能 n=180 勝率57% avg+6.71%，跳空≤1% 才進）")
    elif prev_chg is not None and prev_chg >= 5:
        s += 20; rs.append(f"昨漲 {prev_chg:+.1f}%（動能初現）")
    else:
        rs.append("無漲停動能")
    if t60 == "up":
        s += 20; rs.append("60K 多頭")
    if prev_chg is None or prev_chg < 5:
        s = int(s * 0.2)
    out["隔日沖"] = (max(0, min(100, s)), rs)

    # ---- 定期定額 ----
    s, rs = 0, []
    if amp < 1.5:
        s += 40; rs.append(f"振幅 {amp:.1f}%（<1.5% 低波動適累積）")
    elif amp < 2:
        s += 20; rs.append(f"振幅 {amp:.1f}%")
    else:
        rs.append(f"振幅 {amp:.1f}%（偏高，非定投型）")
    if vol_lots >= 1000:
        s += 25; rs.append(f"日均量 {vol_lots:,.0f} 張（大型流動性）")
    if price < 100:
        s += 20; rs.append(f"股價 {price}（整張門檻 {lot_value/10000:.1f} 萬，低）")
    out["定期定額"] = (max(0, min(100, s)), rs)

    # ---- 零股存股 ----
    s, rs = 0, []
    if lot_value >= 150000:
        s += 50; rs.append(f"整張門檻 {lot_value/10000:.0f} 萬（高價股，零股參與）")
    elif lot_value >= 80000:
        s += 25; rs.append(f"整張門檻 {lot_value/10000:.1f} 萬")
    if amp < 2:
        s += 30; rs.append(f"振幅 {amp:.1f}%（穩定）")
    if t60 == "up":
        s += 20; rs.append("60K 多頭（存股順勢）")
    out["零股存股"] = (max(0, min(100, s)), rs)

    best = max(out.items(), key=lambda kv: kv[1][0])
    return out, best[0], best[1][0], best[1][1]


# ---------------- 主流程 ----------------

def run(top=None, style_filter=None):
    conn = tdb.connect()
    conn.row_factory = tdb.sqlite3.Row
    ls, ls_date = longshort_tags()
    symbols = set(ls.keys())
    for r in conn.execute("SELECT DISTINCT symbol FROM trades").fetchall():
        symbols.add(r["symbol"])
    conn.close()
    if not symbols:
        print("（無標的 — 多空清單與 trades.db 皆空）")
        return

    # 價格源：先試 Fubon（盤中/週一後），失敗退 tw_stock.db
    bars_map = {}
    try:
        os.environ.setdefault("FUBON_USE_TEST", "1")
        import fubon_orders as fo
        fo._load_dotenv()
        sdk, acc = fo.connect_sdk(fo.fubon_cfg())
        for sym in sorted(symbols):
            try:
                bars = fo.fetch_fubon_history(sym, days_back=14, timeframe="5") or []
                if bars:
                    bars_map[sym] = bars
            except BaseException:
                pass
    except BaseException:
        pass
    if len(bars_map) < len(symbols) // 2:
        log.info("Fubon 行情不足 → 退回 tw_stock.db 日K")
        for sym, bars in bars_from_twdb().items():
            bars_map.setdefault(sym, bars)

    t60_cache = {}
    try:
        import fubon_orders as fo
        sdk, acc = fo.connect_sdk(fo.fubon_cfg())
        for sym in sorted(symbols):
            r = fo.trend_60k(sdk, sym)
            if r:
                t60_cache[sym] = r.get("trend")
    except BaseException:
        log.info("60K 趨勢略過（無 SDK 連線）")

    results = []
    for sym in sorted(symbols):
        bars = bars_map.get(sym)
        if not bars:
            continue
        feat = features_from_bars(bars)
        if not feat:
            continue
        dt = dt_stats(tdb.connect(), sym)
        styles, best_style, best_score, best_reasons = score_styles(
            feat, dt, ls.get(sym, {}), t60_cache.get(sym))
        results.append({
            "symbol": sym, "best": best_style, "score": best_score,
            "reasons": best_reasons,
            "styles": {k: v[0] for k, v in styles.items()},
            "all_reasons": {k: v[1] for k, v in styles.items()},
            "feat": {k: v for k, v in feat.items() if k != "closes"},
            "dt": dt, "bias": (ls.get(sym) or {}).get("bias"),
        })

    results.sort(key=lambda r: -r["score"])
    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    payload = {"generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
               "ls_date": ls_date, "results": results}
    json.dump(payload, open(OUT_JSON, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    # 終端表格
    show = [r for r in results if not style_filter or r["best"] == style_filter]
    if top:
        show = show[:top]
    print(f"型態配置（{payload['generated']}｜{len(results)} 檔）"
          + (f"｜型態篩選：{style_filter}" if style_filter else ""))
    print(f"{'標的':<6}{'最適型態':<8}{'分數':>5}{'振幅':>7}{'價':>8}{'整張(萬)':>9}"
          f"{'當沖實測':>14}  主要理由")
    print("-" * 100)
    for r in show:
        dt_s = f"{r['dt']['win_rate']:.0%}(n{r['dt']['n']})" if r["dt"]["n"] else "—"
        reason = r["reasons"][0] if r["reasons"] else ""
        print(f"{r['symbol']:<6}{r['best']:<8}{r['score']:>5}{r['feat']['amp']:>6.1f}%"
              f"{r['feat']['price']:>8}{r['feat']['lot_value']/10000:>8.1f}萬"
              f"{dt_s:>14}  {reason}")
    print(f"\n已寫入 {OUT_JSON}")
    return results


def main():
    ap = argparse.ArgumentParser(description="型態配置引擎")
    ap.add_argument("--style", default=None, help="篩選：當沖/隔日沖/定期定額/零股存股")
    ap.add_argument("--top", type=int, default=None)
    a = ap.parse_args()
    run(top=a.top, style_filter=a.style)


if __name__ == "__main__":
    main()
