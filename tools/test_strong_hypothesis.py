# -*- coding: utf-8 -*-
"""強勢標的假說樣本外驗證：分組對決（90 天真實資料）
假說（來自 10/16~10/30 獲利批次）：高價（≥100 元）＋高波動（日均振幅 ≥2%）的標的，
長黑K追空策略的勝率與期望值顯著較高。
分類基準：chips 籌碼資料的股價與近 5 日振幅（標的特征，非交易期間內動態）"""
import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"C:\Users\Chyau\Documents\預設專案\fubon-daytrade")

from logutil import get_logger  # noqa: E402

log = get_logger("hypo")

SYMBOLS = ["2301", "2382", "2454", "2883", "3231", "2303", "3008", "1605",
           "2327", "2345", "2308", "2357", "2360",
           "1590", "6505", "1301", "1303", "1326", "2395", "2408", "3653",
           "3665", "5871"]

import fubon_orders as fo    # noqa: E402
import backtest as bt        # noqa: E402
import strategy              # noqa: E402
import chips as chips_mod    # noqa: E402

D = chips_mod.load_dataset()
sdk, acc = fo.connect_sdk(fo.fubon_cfg())
try:
    sdk.init_realtime()
except Exception:
    pass

cfg = {**strategy.DEFAULTS}
groups = {"strong": [], "weak": []}
cls_map = {}
for sym in SYMBOLS:
    try:
        info = chips_mod.stock_summary(D, sym)
        if not info or not info.get("close"):
            print(f"{sym}: 無籌碼資料，略過")
            continue
        strong = (info["close"] >= 100 and (info.get("amp5d") or 0) >= 2)
        cls_map[sym] = {"strong": strong, "price": info["close"],
                        "amp5d": info.get("amp5d"), "volume": info.get("volume")}
        bars = fo.fetch_fubon_history(sym, days_back=90, log=None)
        if not bars:
            print(f"{sym}: 無 5 分K，略過")
            continue
        t = bt.run_backtest(bars, cfg, sym, "HYP", log, save=False, use_rules=False)
        groups["strong" if strong else "weak"] += t
        s = sum(x["net_pnl"] or 0 for x in t)
        print(f"{sym}: {'⭐強勢' if strong else '  非強勢'}｜價 {info['close']}｜"
              f"振幅 {info.get('amp5d')}%｜{len(t)} 筆 {s:+,.0f}")
    except Exception as e:
        print(f"{sym}: 錯誤 {str(e)[:60]}")

try:
    sdk.logout()
except Exception:
    pass

print()
print("=" * 70)
print("強勢標的假說驗證（90 天真實資料，分類＝價≥100 且 振幅≥2%）")
print("=" * 70)
for name, ts in (("⭐ 強勢組（價≥100＋振幅≥2%）", groups["strong"]),
                 ("   非強勢組", groups["weak"])):
    if not ts:
        print(f"{name}: 0 筆")
        continue
    p = bt.performance(ts)
    pf = "INF" if p["pf"] == float("inf") else f"{p['pf']:.2f}"
    rev = sum(1 for t in ts if t["result_tag"] == "R_STOP_REVERSAL")
    print(f"{name:<18}{p['n']:>5} 筆｜勝率 {p['win_rate']:5.1f}%｜假跌破率 {rev/len(ts)*100:5.1f}%"
          f"｜淨損益 {p['total']:>+12,.0f}｜PF {pf}｜期望值 {p['avg']:>+8,.0f}｜平均R {p['avg_r']:+.2f}")
print("-" * 70)
print("判讀：強勢組勝率/期望值顯著高於非強勢組 → 假說成立（標的選擇是槓桿）")
