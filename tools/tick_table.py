# -*- coding: utf-8 -*-
"""跳幾檔才獲利？— 當沖成本懶人表（來源：逐字稿 2026-11 當沖教學 §六-1）

費用模型與回測引擎完全一致（strategy.DEFAULTS）：
  手續費＝買賣雙腿各 max(20, 成交金額×0.001425×折扣)，預設三折 0.3
  證交稅＝賣出腿 ×0.0015（現股當沖減半）
  → 每跳一檔的張數價值 = tick × 1000 股；break-even 檔數 = ceil(總成本 / 每檔價值)

用法：
  python tools/tick_table.py              ＃ 標準表（1 張、三折）
  python tools/tick_table.py --discount 0.28 --lots 2
"""
import argparse
import io
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from logutil import ensure_utf8_stdout

ensure_utf8_stdout()

FEE_RATE = 0.001425
TAX_RATE = 0.0015      # 當沖減半
MIN_FEE = 20.0

# 台股 tick 級距：價格 → 一檔元數
def tick_size(px):
    if px < 10:
        return 0.01
    if px < 50:
        return 0.05
    if px < 100:
        return 0.1
    if px < 500:
        return 0.5
    if px < 1000:
        return 1.0
    return 5.0

REPRESENTATIVE = [8, 15, 30, 45, 60, 80, 95, 120, 200, 300, 450, 550, 700, 900,
                  1000, 1500, 2000, 3000, 5000]


def main():
    ap = argparse.ArgumentParser(description="當沖跳檔獲利懶人表")
    ap.add_argument("--discount", type=float, default=0.3, help="手續費折扣（預設 0.3=三折）")
    ap.add_argument("--lots", type=int, default=1, help="張數（預設 1）")
    a = ap.parse_args()
    shares = a.lots * 1000
    print(f"當沖成本懶人表｜手續費 {a.discount*10:.1f} 折（率 {FEE_RATE}，單邊低消 {MIN_FEE:.0f}）"
          f"｜當沖稅 {TAX_RATE:.2%}｜{a.lots} 張（{shares:,} 股）")
    print("=" * 78)
    print(f"{'參考價':>8}{'一檔':>8}{'雙腿手續費':>12}{'證交稅':>10}{'總成本':>10}"
          f"{'每檔價值':>10}{'損平檔數':>10}{'第2檔起淨賺/檔':>14}")
    print("-" * 78)
    for px in REPRESENTATIVE:
        tick = tick_size(px)
        val = px * shares
        fee = (max(MIN_FEE, val * FEE_RATE * a.discount)
               + max(MIN_FEE, val * FEE_RATE * a.discount))
        tax = val * TAX_RATE
        cost = fee + tax
        per_tick = tick * shares
        be = math.ceil(cost / per_tick)
        print(f"{px:>8}{tick:>8}{fee:>12,.0f}{tax:>10,.0f}{cost:>10,.0f}"
              f"{per_tick:>10,.0f}{be:>9} 檔{per_tick:>12,.0f}")
    print("-" * 78)
    print("※ 損平檔數＝進出各同一價時需跳幾檔才回本；之後每多跳一檔全額入袋。")
    print("※ 停損距離若小於損平檔數×一檔，表示風險報酬比天然差 → 換標的或拉大格局。")
    print("※ 引擎 min_lot_risk=600 的意義：單張停損距離＋一檔的價值需 ≥600，")
    print("   確保成本相對風險夠小（同逐字稿：價差要能 cover 成本）。")


if __name__ == "__main__":
    main()
