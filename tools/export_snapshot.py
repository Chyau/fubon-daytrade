# -*- coding: utf-8 -*-
"""export_snapshot.py — 產生唯讀快照站（部署到 Vercel 的靜態頁面）

從 data/trades.db 讀取交易紀錄，生成 snapshot/index.html：
  標題統計（筆數／勝率／淨損益／獲利因子／平均R）＋淨值曲線（SVG）
  ＋每檔標的統計＋出場標籤分佈＋最近交易表＋核准規則

設計原則：
  - 純標準庫（sqlite3/html/datetime），零外部相依
  - 不含任何憑證／帳號資訊；排除 is_dup=1 與 DEMO 樣本
  - 產出後由 push_snapshot.bat 提交到 GitHub → Vercel 自動部署

用法：python tools/export_snapshot.py [--out snapshot] [--recent 30]
"""
import argparse
import html
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(__file__).resolve().parent.parent
DB = BASE / "data" / "trades.db"


def load_trades(conn):
    cols = [c[1] for c in conn.execute("PRAGMA table_info(trades)")]
    rows = []
    for r in conn.execute("SELECT * FROM trades ORDER BY id"):
        d = dict(zip(cols, r))
        # 排除重複標記與 DEMO 樣本（與學習迴路口徑一致）
        if d.get("is_dup") == 1:
            continue
        if str(d.get("run_id", "")).startswith("DEMO"):
            continue
        rows.append(d)
    return rows


def perf(trades):
    nets = [t["net_pnl"] or 0 for t in trades]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    gp, gl = sum(wins), -sum(losses)
    rs = [t["r_multiple"] for t in trades if t["r_multiple"] is not None]
    return {
        "n": len(trades),
        "wins": len(wins),
        "win_rate": len(wins) / len(trades) * 100 if trades else 0,
        "total": sum(nets),
        "pf": (gp / gl) if gl > 0 else float("inf"),
        "avg_r": sum(rs) / len(rs) if rs else 0,
    }


def svg_equity(trades, w=860, h=180):
    """淨值曲線（純 SVG，累計淨損益）"""
    pts, cum = [], 0
    for t in trades:
        cum += t["net_pnl"] or 0
        pts.append(cum)
    if len(pts) < 2:
        return "<p class='muted'>樣本不足，無法繪製淨值曲線</p>"
    lo, hi = min(pts + [0]), max(pts + [0])
    rng = (hi - lo) or 1
    pad, pw, ph = 8, w - 16, h - 30
    xy = [(pad + i / (len(pts) - 1) * pw,
           pad + (hi - v) / rng * ph) for i, v in enumerate(pts)]
    poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in xy)
    y0 = pad + (hi - 0) / rng * ph
    last = pts[-1]
    color = "#16a34a" if last >= 0 else "#dc2626"
    return f"""<svg viewBox="0 0 {w} {h}" style="width:100%;max-width:{w}px">
<line x1="{pad}" y1="{y0:.1f}" x2="{w - pad}" y2="{y0:.1f}" stroke="#9ca3af" stroke-dasharray="4 4"/>
<polyline points="{poly}" fill="none" stroke="{color}" stroke-width="2"/>
<text x="{pad}" y="{h - 6}" fill="#6b7280" font-size="12">累計淨損益 {last:+,.0f} 元（{len(pts)} 筆）</text>
</svg>"""


def esc(v):
    return html.escape(str(v if v is not None else "—"))


def money(v):
    return f"{v:+,.0f}" if v is not None else "—"


def build_html(trades, rules, recent_n):
    p = perf(trades)
    pf = "∞" if p["pf"] == float("inf") else f"{p['pf']:.2f}"
    dates = sorted({t["date"] for t in trades if t.get("date")})
    rng = f"{dates[0]} ~ {dates[-1]}" if dates else "—"

    # 每檔標的統計
    per = {}
    for t in trades:
        a = per.setdefault(t["symbol"], [0, 0, 0])
        a[0] += 1
        a[1] += t["net_pnl"] or 0
        a[2] += 1 if (t["net_pnl"] or 0) > 0 else 0
    sym_rows = "".join(
        f"<tr><td><b>{esc(s)}</b></td><td>{a[0]}</td>"
        f"<td>{a[2] / a[0] * 100:.0f}%</td>"
        f"<td class='{'pos' if a[1] >= 0 else 'neg'}'>{money(a[1])}</td></tr>"
        for s, a in sorted(per.items(), key=lambda x: -x[1][1]))

    # 出場標籤分佈
    tags = {}
    for t in trades:
        a = tags.setdefault(t.get("result_tag") or "—", [0, 0])
        a[0] += 1
        a[1] += t["net_pnl"] or 0
    tag_rows = "".join(
        f"<tr><td>{esc(k)}</td><td>{v[0]}</td>"
        f"<td class='{'pos' if v[1] >= 0 else 'neg'}'>{money(v[1])}</td></tr>"
        for k, v in sorted(tags.items(), key=lambda x: -x[1][0]))

    # 最近交易
    recent = trades[-recent_n:][::-1]
    tr_rows = ""
    for t in recent:
        cls = "pos" if (t["net_pnl"] or 0) >= 0 else "neg"
        tr_rows += (f"<tr><td>{esc(t.get('id'))}</td><td>{esc(t.get('date'))}</td>"
                    f"<td><b>{esc(t.get('symbol'))}</b></td>"
                    f"<td>{esc(t.get('direction') or '空')}</td>"
                    f"<td>{esc(t.get('entry_time'))}@{esc(t.get('entry_price'))}</td>"
                    f"<td>{esc(t.get('exit_time'))}@{esc(t.get('exit_price'))}</td>"
                    f"<td>{esc(t.get('exit_reason'))}</td>"
                    f"<td>{esc(t.get('result_tag'))}</td>"
                    f"<td class='{cls}'>{money(t.get('net_pnl'))}</td>"
                    f"<td>{esc(round(t['r_multiple'], 2) if t.get('r_multiple') is not None else '—')}</td></tr>")

    # 核准規則
    rule_html = ""
    if rules:
        items = "".join(
            f"<div>• <b>{esc(r['name'])}</b>｜{esc((r.get('note') or '')[:80])}</div>"
            for r in rules)
        rule_html = f"<div class='card'><h2>📏 已核准避坑規則（{len(rules)} 條）</h2>{items}</div>"

    gen = datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"""<!DOCTYPE html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>佛系當沖量化系統 — 唯讀快照</title>
<style>
body{{font-family:"Microsoft JhengHei",system-ui,sans-serif;max-width:960px;margin:24px auto;padding:0 16px;background:#f8fafc;color:#111827}}
h1{{font-size:1.5rem}} h2{{font-size:1.1rem;border-left:4px solid #2563eb;padding-left:8px}}
.card{{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:14px 18px;margin:14px 0}}
table{{border-collapse:collapse;width:100%;font-size:.9rem}}
th,td{{border-bottom:1px solid #e5e7eb;padding:5px 8px;text-align:left}}
th{{background:#f1f5f9}}
.pos{{color:#16a34a}} .neg{{color:#dc2626}} .muted{{color:#6b7280;font-size:.85rem}}
.stats{{display:flex;flex-wrap:wrap;gap:18px}} .stats div{{min-width:120px}}
.stats b{{display:block;font-size:1.25rem}}
footer{{text-align:center;color:#9ca3af;font-size:.8rem;margin:24px 0}}
</style></head><body>
<h1>⚡ 佛系當沖量化系統 — 唯讀快照</h1>
<p class="muted">資料區間 {esc(rng)}｜本頁為本機系統每日自動推送的靜態報告（僅供檢視，不含任何帳號資訊）</p>
<div class="card"><div class="stats">
<div>總交易<b>{p['n']}</b></div><div>勝率<b>{p['win_rate']:.1f}%</b></div>
<div>總淨損益<b class="{'pos' if p['total'] >= 0 else 'neg'}">{money(p['total'])}</b></div>
<div>獲利因子<b>{pf}</b></div><div>平均 R<b>{p['avg_r']:+.2f}</b></div>
</div></div>
<div class="card"><h2>📈 淨值曲線</h2>{svg_equity(trades)}</div>
<div class="card"><h2>🧩 標的統計（依淨損益排序）</h2>
<table><tr><th>標的</th><th>筆數</th><th>勝率</th><th>淨損益</th></tr>{sym_rows}</table></div>
<div class="card"><h2>🏷 出場標籤分佈</h2>
<table><tr><th>標籤</th><th>筆數</th><th>淨損益</th></tr>{tag_rows}</table></div>
{rule_html}
<div class="card"><h2>📋 最近 {len(recent)} 筆交易</h2>
<table><tr><th>#</th><th>日期</th><th>標的</th><th>方向</th><th>進場</th><th>出場</th><th>原因</th><th>標籤</th><th>淨損益</th><th>R</th></tr>
{tr_rows}</table></div>
<footer>由 tools/export_snapshot.py 於 {esc(gen)} 生成｜GitHub 自動推送 → Vercel 部署</footer>
</body></html>"""


def main():
    ap = argparse.ArgumentParser(description="產生唯讀快照站（Vercel 靜態部署）")
    ap.add_argument("--out", default=str(BASE / "snapshot"), help="輸出資料夾")
    ap.add_argument("--recent", type=int, default=30, help="最近交易筆數")
    a = ap.parse_args()

    if not DB.exists():
        print(f"找不到資料庫：{DB}")
        return 1
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    trades = load_trades(conn)
    try:
        rules = [dict(r) for r in conn.execute(
            "SELECT * FROM rules WHERE approved=1 ORDER BY id").fetchall()]
    except sqlite3.OperationalError:
        rules = []
    conn.close()

    out = Path(a.out)
    out.mkdir(exist_ok=True)
    html_text = build_html(trades, rules, a.recent)
    (out / "index.html").write_text(html_text, encoding="utf-8")

    p = perf(trades)
    print(f"✓ 快照已生成：{out / 'index.html'}")
    print(f"  交易 {p['n']} 筆｜勝率 {p['win_rate']:.1f}%｜淨損益 {p['total']:+,.0f}")
    print("  下一步：執行 push_snapshot.bat 提交到 GitHub（Vercel 自動部署）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
