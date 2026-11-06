# -*- coding: utf-8 -*-
"""內外盤虛擬帳戶統計 — 評估 A 訊號（TREND/DIVERGE）能否升級實單

資料源：signals_log 的 OF_PAPER_OPEN / OF_PAPER_CLOSE（live 引擎持久化）。
配對規則：同一標的按時間排序，OPEN 之後第一個 CLOSE 配成一筆；
跨重啟未平倉的 OPEN 計為「棄單」（不計損益）。

升級門檻（鐵律：觀察→驗證→實戰）：
  任一訊號類型 n≥30、勝率≥50%、平均報酬 ≥ +0.15%（雙腿成本約 0.1~0.15%）
  → 建議「先沙箱實單小額驗證」，再考慮正式。

用法：
  python tools/of_paper_stats.py           ＃ 全部統計
  python tools/of_paper_stats.py --min-n 30
"""
import argparse
import io
import json
import os
import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from logutil import ensure_utf8_stdout

ensure_utf8_stdout()

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "data", "trades.db")


def main():
    ap = argparse.ArgumentParser(description="內外盤虛擬帳戶統計")
    ap.add_argument("--db", default=DB)
    ap.add_argument("--min-n", type=int, default=30, help="升級建議的樣本門檻")
    a = ap.parse_args()

    conn = sqlite3.connect(a.db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT date, symbol, ts, event, reason, detail FROM signals_log "
        "WHERE event IN ('OF_PAPER_OPEN','OF_PAPER_CLOSE') ORDER BY rowid").fetchall()
    if not rows:
        print("（尚無內外盤虛擬交易紀錄 — 引擎跑一段時間後再來看）")
        return

    open_by_sym = {}
    paired = []          # (kind, dir, pnl_pct, why, date)
    orphan = 0
    for r in rows:
        try:
            d = json.loads(r["detail"] or "{}")
        except Exception:
            d = {}
        if r["event"] == "OF_PAPER_OPEN":
            open_by_sym[r["symbol"]] = d
        else:
            op = open_by_sym.pop(r["symbol"], None)
            if op is None:
                continue        # 無配對的平倉（舊資料），略
            if "pnl_pct" in d:
                paired.append((d.get("kind") or op.get("kind"),
                               d.get("dir") or op.get("dir"),
                               d["pnl_pct"], d.get("why", "?"), r["date"]))
            else:
                paired.append((op.get("kind"), op.get("dir"), None, "無數據", r["date"]))
    orphan = len(open_by_sym)

    total = len(paired) + orphan
    print(f"內外盤虛擬帳戶：已平倉 {len(paired)} 筆｜棄單（重啟未平倉）{orphan} 筆\n")

    by_kind = defaultdict(list)
    for kind, d, pnl, why, date in paired:
        if pnl is not None:
            by_kind[(kind, d)].append((pnl, why))

    print(f"{'訊號':<22}{'n':>5}{'勝率':>7}{'平均%':>9}{'停損':>6}{'達標':>6}{'反訊號':>7}")
    print("-" * 66)
    best = None
    for (kind, d), rs in sorted(by_kind.items(), key=lambda x: -len(x[1])):
        n = len(rs)
        wr = sum(1 for p, _ in rs if p > 0) / n
        av = sum(p for p, _ in rs) / n
        ns = sum(1 for _, w in rs if w == "停損")
        nt = sum(1 for _, w in rs if w == "達標")
        nf = sum(1 for _, w in rs if w == "反訊號")
        print(f"{kind+' '+d:<22}{n:>5}{wr:>7.0%}{av:>+8.2f}%{ns:>6}{nt:>6}{nf:>7}")
        if best is None or av > best[1]:
            best = ((kind, d), av, n, wr)
    print("-" * 66)

    if not by_kind:
        print("尚無已平倉樣本")
        return
    if best:
        (kind, d), av, n, wr = best
        ok = n >= a.min_n and wr >= 0.5 and av >= 0.15
        print(f"最佳：{kind} {d}｜n={n} 勝率 {wr:.0%} 平均 {av:+.2f}%")
        if ok:
            print(f"✓ 達升級門檻（n≥{a.min_n}、勝率≥50%、平均≥+0.15%）"
                  f"→ 建議沙箱小額實單驗證（OF_AUTOTRADE 機制）")
        else:
            need = []
            if n < a.min_n:
                need.append(f"樣本 {n}/{a.min_n}")
            if wr < 0.5:
                need.append(f"勝率 {wr:.0%}<50%")
            if av < 0.15:
                need.append(f"平均 {av:+.2f}%<+0.15%")
            print(f"✗ 未達門檻（{'、'.join(need)}）→ 繼續觀察，不實單")
    conn.close()


if __name__ == "__main__":
    main()
