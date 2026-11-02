# -*- coding: utf-8 -*-
"""nextday_buy_screener.py — 隔日沖買進選股器（逐字稿：隔日沖SOP）

盤後執行：篩選今日符合下列全部條件的標的（明天開盤開高即出場的買進候選）：
  ① 流動性門檻：成交量 ≥ 2000 張（周轉率的實務近似 — yf 無流通股數，誠實標注）
  ② 量能放大：今日量 ≥ 1.5 × MA5(量)
  ③ 收盤站上布林上軌 (20, 2)
  ④ 收盤 ≥ 97% × 當日高點（收在相對高點）
  ⑤ 紅K（收 > 開）

預設宇宙：多空分類 DB 的「做多＋偏多」清單（long-bias 標的）；
可用 --symbols 覆寫、--all 檢查多空全部 41 檔。
輸出：console + data/nextday_buy.json（date／candidates／conditions）

用法：python tools/nextday_buy_screener.py [--symbols 2330,2426] [--all]
"""
import argparse
import io
import json
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import yfinance as yf  # noqa: E402
import longshort       # noqa: E402


def screen_daily(df, cfg):
    """對單檔日線 DataFrame 跑五條件篩選。回傳 (ok, detail)"""
    import statistics
    if df is None or df.empty or len(df) < 25:
        return False, {"reason": "歷史不足"}
    if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
        df.columns = df.columns.get_level_values(0)
    closes = [float(x) for x in df["Close"].tolist()]
    vols = [float(x or 0) for x in df["Volume"].tolist()]
    highs = [float(x) for x in df["High"].tolist()]
    opens = [float(x) for x in df["Open"].tolist()]
    c, v = closes[-1], vols[-1]
    h, o = highs[-1], opens[-1]
    period = int(cfg.get("nd_bb_period", 20))
    win = closes[-period:]
    ma = sum(win) / len(win)
    sd = (sum((x - ma) ** 2 for x in win) / len(win)) ** 0.5
    boll_up = ma + float(cfg.get("nd_bb_dev", 2.0)) * sd
    vma5 = sum(vols[-5:]) / 5
    ok = (c > boll_up
          and v >= float(cfg.get("nd_vol_mult", 1.5)) * vma5
          and c >= float(cfg.get("nd_rel_high", 0.97)) * h
          and c > o
          and v >= float(cfg.get("nd_min_vol_shares", 2_000_000)))
    detail = {"close": round(c, 2), "boll_up": round(boll_up, 2),
              "vol_wan": round(v / 1000), "vma5_wan": round(vma5 / 1000),
              "rel_high": round(c / h * 100, 1), "red_k": c > o}
    return ok, detail


def main():
    ap = argparse.ArgumentParser(description="隔日沖買進選股器（布林上軌×量增×收高點紅K）")
    ap.add_argument("--symbols", default="", help="自訂標的（逗號分隔）")
    ap.add_argument("--all", action="store_true", help="檢查多空全部 41 檔")
    ap.add_argument("--out", default=str(BASE / "data" / "nextday_buy.json"))
    a = ap.parse_args()

    if a.symbols.strip():
        syms = [s.strip().upper() for s in a.symbols.replace("，", ",").split(",")
                if s.strip()]
    else:
        _, rows = longshort.load_longshort()
        by = {}
        for r in rows:
            by.setdefault(r["bias_label"], []).append(r["stock_no"])
        syms = (by.get("做多", []) + by.get("偏多", [])) if not a.all \
            else sorted({r["stock_no"] for r in rows})

    cfg = {}
    cands, checked = [], 0
    print(f"篩選 {len(syms)} 檔（條件：量增≥1.5×MA5 × 布林上軌(20,2) × "
          f"收≥97%日高 × 紅K × 量≥2000張）")
    print("=" * 70)
    for i, sym in enumerate(syms):
        try:
            df = None
            for s in (f"{sym}.TW", f"{sym}.TWO"):
                try:
                    d = yf.download(s, period="60d", interval="1d",
                                    progress=False, auto_adjust=False)
                    if d is not None and not d.empty:
                        df = d
                        break
                except Exception:
                    continue
            ok, detail = screen_daily(df, cfg)
            checked += 1
            mark = "✓ 入選" if ok else ""
            if ok:
                cands.append({"symbol": sym, **detail})
            print(f"  {sym} {mark}｜收 {detail.get('close')}｜布林上軌 "
                  f"{detail.get('boll_up')}｜量 {detail.get('vol_wan')} 張"
                  f"｜相對高點 {detail.get('rel_high')}%")
        except Exception as e:
            print(f"  {sym} 錯誤：{str(e)[:60]}")
        if i < len(syms) - 1:
            time.sleep(0.6)

    print("=" * 70)
    print(f"入選 {len(cands)}／{checked} 檔")
    for c in cands:
        print(f"  ★ {c['symbol']}｜收 {c['close']}｜相對高點 {c['rel_high']}%")
    out = {"generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
           "conditions": "量增≥1.5×MA5 × 布林上軌(20,2) × 收≥97%日高 × 紅K × 量≥2000張",
           "candidates": cands}
    Path(a.out).write_text(json.dumps(out, ensure_ascii=False, indent=1),
                           encoding="utf-8")
    print(f"\n✓ 已輸出 {a.out}")
    print("  明日操作（逐字稿）：開盤開高即出場；未開高依紀律出場。")


if __name__ == "__main__":
    main()
