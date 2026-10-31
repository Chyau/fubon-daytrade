# -*- coding: utf-8 -*-
"""族群輪動報告：ETF 加碼/減碼 ＋ 三大法人 ＋ 輪動候選"""
import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"C:\Users\Chyau\Documents\預設專案\fubon-daytrade")

import etf_flow  # noqa: E402

d = etf_flow.load()
print(f"資料日：{d.get('today_date')}（前一日 {d.get('prev_date')}）｜"
      f"追蹤 ETF {len(d.get('today', {}))} 支｜個股覆蓋 {len(d.get('inst', {}))} 檔")

adds, drops = etf_flow.etf_flow(d)
print("\n========== 🟢 ETF 昨日加碼 Top 12（機構資金流入） ==========")
for code, rec in adds[:12]:
    etfs = "、".join(f"{n}:{dw:+.2f}%" for n, dw in rec["etfs"][:2])
    print(f"  {code} ｜ETF加碼 {rec['add']:+.2f}%｜{etfs}")

print("\n========== 🔴 ETF 昨日減碼 Top 8（機構資金流出） ==========")
for code, rec in drops[:8]:
    etfs = "、".join(f"{n}:{dw:+.2f}%" for n, dw in rec["etfs"][:2])
    print(f"  {code} ｜ETF減碼 {rec['add']:+.2f}%｜{etfs}")

print("\n========== 三大法人買超 Top 10（張） ==========")
for total, code, v in etf_flow.inst_flow(d, 10):
    print(f"  {code} ｜合計 {total:+,} 張（外資 {v.get('foreign', 0):+,}｜"
          f"投信 {v.get('trust', 0):+,}｜自營 {v.get('dealer', 0):+,}）")

print("\n========== 🔄 族群輪動候選（ETF加碼＋法人買超 評分排序） ==========")
cands = etf_flow.rotation_candidates(d, 12)
if not cands:
    print("（今日無符合條件者）")
for c in cands:
    etfs = "、".join(f"{n}:{dw:+.2f}%" for n, dw in c["etfs"][:2])
    print(f"  {c['code']} ｜評分 {c['score']:.2f}｜ETF加碼 {c['add']:+.2f}%｜"
          f"法人 {c['inst_total']:+,} 張｜現價 {c['price']}｜{etfs}")
