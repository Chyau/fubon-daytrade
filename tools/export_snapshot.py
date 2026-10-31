# -*- coding: utf-8 -*-
"""export_snapshot.py — 產生「九分頁唯讀儀表板」（GitHub Pages / Vercel 靜態部署）

分頁（與本機 Flask 儀表板對應）：
  📊 儀表板｜⚡ 自動執行｜📋 交易紀錄｜📈 交易績效｜🧠 學習迴路
  🧲 籌碼選股｜🧾 下單紀錄｜⏱ 回測｜📖 手冊

資料來源：data/trades.db（trades/rules/orders）＋ journal.DIMS（坑點挖掘）
＋ docs/操作手冊.md（內建）＋ data/chips_export.json（籌碼，另由 export_chips.py 生成）

設計原則：
  - 產出端純標準庫；前端零相依（無 CDN），可被 lock_snapshot.py 整頁加密
  - 排除 is_dup=1 與 DEMO 樣本；不含任何憑證／帳號資訊
  - Actions 重放後自動重新生成 → Pages/Vercel 自動部署

用法：python tools/export_snapshot.py [--out snapshot]
"""
import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import db as tdb        # noqa: E402
import strategy         # noqa: E402
from journal import DIMS  # noqa: E402

DB = BASE / "data" / "trades.db"


# ── 資料載入 ──────────────────────────────────────────────
def load_trades(conn):
    cols = [c[1] for c in conn.execute("PRAGMA table_info(trades)")]
    rows = []
    for r in conn.execute("SELECT * FROM trades ORDER BY id"):
        d = dict(zip(cols, r))
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
    return {"n": len(trades), "wins": len(wins),
            "win_rate": len(wins) / len(trades) * 100 if trades else 0,
            "total": sum(nets),
            "pf": round(gp / gl, 2) if gl > 0 else None,
            "avg_r": round(sum(rs) / len(rs), 2) if rs else 0}


def mine_pits(conn, min_n=4, gap=0.2):
    """複刻 journal.cmd_pits 的挖掘邏輯，回傳 (基準虧損率, 坑點清單, 全維度分組)"""
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM trades WHERE COALESCE(is_dup,0)=0 "
        "AND run_id NOT LIKE 'DEMO%'").fetchall()]
    if not rows:
        return None, [], []
    base = sum(1 for r in rows if (r["net_pnl"] or 0) <= 0) / len(rows)
    pits, groups = [], []
    for dim, fn in DIMS.items():
        buckets = {}
        for r in rows:
            try:
                buckets.setdefault(fn(r), []).append(r)
            except Exception:
                buckets.setdefault("?", []).append(r)
        for key, rs in buckets.items():
            n = len(rs)
            if n < min_n:
                continue
            n_loss = sum(1 for r in rs if (r["net_pnl"] or 0) <= 0)
            lr = n_loss / n
            avg_r = sum(r["r_multiple"] or 0 for r in rs) / n
            is_pit = (lr - base >= gap and avg_r < 0)
            groups.append({"dim": dim, "key": str(key), "n": n, "n_loss": n_loss,
                           "lr": round(lr * 100, 1), "avg_r": round(avg_r, 2),
                           "pit": is_pit})
            if is_pit:
                pits.append({"dim": dim, "key": str(key), "n": n,
                             "lr": round(lr * 100, 1), "avg_r": round(avg_r, 2)})
    groups.sort(key=lambda g: (-int(g["pit"]), g["dim"]))
    pits.sort(key=lambda p: p["avg_r"])
    return round(base * 100, 1), pits, groups


def perf_extra(trades):
    """交易績效頁的進階統計"""
    daily = {}
    for t in trades:
        a = daily.setdefault(t["date"], [0, 0])
        a[0] += 1
        a[1] += t["net_pnl"] or 0
    daily_rows = [{"date": d, "n": v[0], "net": round(v[1])}
                  for d, v in sorted(daily.items())]
    # 最大回撤（依日累計）
    cum, peak, mdd = 0, 0, 0
    for r in daily_rows:
        cum += r["net"]
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    wd_names = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    wd = {}
    for t in trades:
        try:
            k = wd_names[datetime.strptime(t["date"], "%Y-%m-%d").weekday()]
        except Exception:
            continue
        a = wd.setdefault(k, [0, 0, 0])
        a[0] += 1
        a[1] += t["net_pnl"] or 0
        a[2] += 1 if (t["net_pnl"] or 0) > 0 else 0
    r_buckets = [("<-2", -99, -2), ("-2~-1", -2, -1), ("-1~0", -1, 0),
                 ("0~1", 0, 1), ("1~2", 1, 2), ("2~5", 2, 5), (">5", 5, 99)]
    rb = []
    for label, lo, hi in r_buckets:
        ts = [t for t in trades if t["r_multiple"] is not None
              and lo <= t["r_multiple"] < hi]
        if ts:
            rb.append({"bucket": label, "n": len(ts),
                       "net": round(sum(t["net_pnl"] or 0 for t in ts))})
    hb = []
    for label, lo, hi in [("0-10根", 0, 11), ("11-20根", 11, 21),
                          ("21-40根", 21, 41), (">40根", 41, 9999)]:
        ts = [t for t in trades if (t.get("bars_held") or 0) and lo <= t["bars_held"] < hi]
        if ts:
            hb.append({"bucket": label, "n": len(ts), "win": round(
                sum(1 for t in ts if (t["net_pnl"] or 0) > 0) / len(ts) * 100),
                "net": round(sum(t["net_pnl"] or 0 for t in ts))})
    return {"daily": daily_rows, "mdd": round(mdd),
            "weekday": [{"wd": k, "n": v[0],
                         "win": round(v[2] / v[0] * 100) if v[0] else 0,
                         "net": round(v[1])} for k, v in wd.items()],
            "r_buckets": rb, "hold": hb}


def md_to_html(md):
    import html as _h
    out, in_code, in_table = [], False, False
    for ln in md.splitlines():
        if ln.strip().startswith("```"):
            out.append("</pre>" if in_code else "<pre>")
            in_code = not in_code
            continue
        if in_code:
            out.append(_h.escape(ln))
            continue
        if ln.startswith("|") and ln.count("|") >= 2:
            cells = [c.strip() for c in ln.strip("|").split("|")]
            if cells and all(set(c) <= set("-: ") for c in cells if c):
                continue
            if not in_table:
                out.append("<table>")
                in_table = True
            tag = "th" if not in_table else "td"
            out.append("<tr>" + "".join(
                f"<{tag}>{_h.escape(c)}</{tag}>" for c in cells) + "</tr>")
            continue
        if in_table:
            out.append("</table>")
            in_table = False
        m = re.match(r"^(#{1,4})\s+(.*)", ln)
        if m:
            lvl = min(4, len(m.group(1)) + 1)
            out.append(f"<h{lvl}>{_h.escape(m.group(2))}</h{lvl}>")
            continue
        if ln.startswith("- ") or ln.startswith("* "):
            out.append(f"• {_h.escape(ln[2:])}")
            continue
        if not ln.strip():
            continue
        s = _h.escape(ln)
        s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"`(.+?)`", r"<code>\1</code>", s)
        out.append(f"<p>{s}</p>")
    if in_table:
        out.append("</table>")
    if in_code:
        out.append("</pre>")
    return "\n".join(out)


def build_spa(trades, rules, pits, pit_base, groups, orders, extra, manual_html, chips_ok):
    p = perf(trades)
    dates = sorted({t["date"] for t in trades if t.get("date")})
    gen = datetime.now().strftime("%Y-%m-%d %H:%M")

    runs = {}
    for t in trades:
        runs.setdefault(t.get("run_id") or "—", []).append(t)
    run_rows = []
    for rid, ts in sorted(runs.items(), key=lambda x: -len(x[1])):
        rp = perf(ts)
        kind = ("live" if rid.startswith("LIVE-")
                else "paper" if rid.startswith("PAPER-") else "bt")
        run_rows.append({"id": rid, "kind": kind, **rp,
                         "dates": f"{min(t['date'] for t in ts)}~{max(t['date'] for t in ts)}" if ts else ""})

    slim = [{"id": t["id"], "d": t.get("date"), "s": t.get("symbol"),
             "dir": t.get("direction") or "short",
             "et": t.get("entry_time"), "ep": t.get("entry_price"),
             "xt": t.get("exit_time"), "xp": t.get("exit_price"),
             "xr": t.get("exit_reason"), "tag": t.get("result_tag"),
             "pnl": t.get("net_pnl"),
             "r": (round(t["r_multiple"], 2) if t.get("r_multiple") is not None else None),
             "rid": t.get("run_id")} for t in trades]

    data = {"meta": {"generated": gen,
                     "range": f"{dates[0]} ~ {dates[-1]}" if dates else "", "n": p["n"]},
            "perf": p, "runs": run_rows, "trades": slim,
            "rules": [{"name": r["name"], "approved": r.get("approved"),
                       "note": (r.get("note") or "")[:100]} for r in rules],
            "pits": {"base": pit_base, "found": pits, "groups": groups},
            "orders": [{"id": o["id"], "ts": o.get("ts"), "sym": o.get("symbol"),
                        "side": o.get("side"), "qty": o.get("quantity"),
                        "ok": o.get("is_success"), "msg": (o.get("message") or "")[:60]}
                       for o in orders],
            "extra": extra,
            "chips_ok": chips_ok}

    return PAGE.replace("__DATA__", json.dumps(data, ensure_ascii=False)) \
               .replace("__MANUAL__", manual_html)


# ── SPA 模板 ─────────────────────────────────────────────
PAGE = """<!DOCTYPE html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>⚡ 佛系當沖 — 雲端儀表板</title>
<style>
:root{--bg:#0b0e14;--card:#161a22;--bd:#272c37;--tx:#e8eaed;--sub:#8b93a3;
--grn:#2ecc71;--red:#e74c3c;--yel:#f1b143;--acc:#4ea1f3}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--tx);font-size:14px;
font-family:-apple-system,'Segoe UI','PingFang TC','Microsoft JhengHei',sans-serif;-webkit-font-smoothing:antialiased}
header{background:rgba(17,21,29,.94);position:sticky;top:0;z-index:50;border-bottom:1px solid var(--bd);
padding:10px 16px;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
header h1{font-size:15px;font-weight:700;background:linear-gradient(90deg,var(--acc),#7c5cff);
-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;white-space:nowrap}
nav{display:flex;gap:3px;flex-wrap:wrap}
nav a{color:var(--sub);text-decoration:none;padding:5px 11px;border-radius:8px;font-size:12.5px;cursor:pointer;transition:.18s}
nav a:hover{background:#1b2029;color:var(--tx)}
nav a.active{background:linear-gradient(135deg,var(--acc),#7c5cff);color:#fff}
main{padding:16px 18px;max-width:1200px;margin:0 auto}
.sec{display:none}.sec.active{display:block}
.card{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:14px 16px;margin-bottom:14px}
.grid{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:14px}
@media(max-width:760px){.grid{grid-template-columns:repeat(2,1fr)}}
.kpi .l{font-size:11px;color:var(--sub);letter-spacing:.5px;margin-bottom:5px}
.kpi .v{font-size:21px;font-weight:700;font-variant-numeric:tabular-nums}
.pos{color:var(--grn)}.neg{color:var(--red)}.muted{color:var(--sub)}
h2{font-size:14px;margin:2px 0 10px;padding-left:10px;border-left:3px solid var(--acc)}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{background:#13161d;color:var(--sub);font-weight:500;padding:7px 9px;text-align:left;
border-bottom:1px solid var(--bd);position:sticky;top:0;z-index:1}
td{padding:6px 9px;border-bottom:1px solid #1e2128}
tr:hover td{background:#1e2330}
.tbl-wrap{max-height:520px;overflow:auto;border:1px solid var(--bd);border-radius:8px;
-webkit-overflow-scrolling:touch}
input,select{background:#13161d;color:var(--tx);border:1px solid var(--bd);border-radius:6px;
padding:6px 10px;font-size:13px}
.badge{display:inline-block;padding:1px 7px;border-radius:4px;font-size:11px;font-weight:600}
.b-live{background:rgba(46,204,113,.15);color:var(--grn)}
.b-paper{background:rgba(78,161,243,.15);color:var(--acc)}
.b-bt{background:rgba(139,147,163,.12);color:var(--sub)}
.b-x{background:rgba(231,76,60,.15);color:var(--red)}
.b-pit{background:rgba(231,76,60,.2);color:var(--red);font-weight:700}
.b-ok{background:rgba(46,204,113,.15);color:var(--grn)}
footer{text-align:center;color:var(--sub);font-size:11px;padding:20px}
.frow{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:12px;align-items:center}
.krow{display:flex;gap:8px;flex-wrap:wrap}
.kbd{background:#1e2330;border:1px solid var(--bd);border-radius:5px;padding:2px 9px;font-size:12px;cursor:pointer}
.kbd.on{background:var(--acc);color:#fff;border-color:var(--acc)}
svg{width:100%;height:auto;display:block}
pre{background:#13161d;border:1px solid var(--bd);border-radius:8px;padding:10px 12px;
overflow:auto;font-size:12px;line-height:1.6}
code{background:#1e2330;border-radius:3px;padding:1px 5px;font-size:12px}
h3,h4{margin:12px 0 6px}
p{margin:5px 0;line-height:1.7;font-size:13px}
.bar{height:14px;background:#13161d;border-radius:4px;overflow:hidden;margin-top:3px}
.bar>div{height:100%;border-radius:4px}
</style></head><body>
<header><h1>⚡ 佛系當沖 — 雲端儀表板</h1>
<nav>
<a class="active" data-s="ov" onclick="tab('ov')">📊 儀表板</a>
<a data-s="au" onclick="tab('au')">⚡ 自動執行</a>
<a data-s="tr" onclick="tab('tr')">📋 交易紀錄</a>
<a data-s="pf" onclick="tab('pf')">📈 交易績效</a>
<a data-s="ln" onclick="tab('ln')">🧠 學習迴路</a>
<a data-s="ch" onclick="tab('ch')">🧲 籌碼選股</a>
<a data-s="or" onclick="tab('or')">🧾 下單紀錄</a>
<a data-s="bt" onclick="tab('bt')">⏱ 回測</a>
<a data-s="mn" onclick="tab('mn')">📖 手冊</a>
</nav></header>
<main>
<div id="ov" class="sec active"></div>
<div id="au" class="sec"></div>
<div id="tr" class="sec"></div>
<div id="pf" class="sec"></div>
<div id="ln" class="sec"></div>
<div id="ch" class="sec"></div>
<div id="or" class="sec"></div>
<div id="bt" class="sec"></div>
<div id="mn" class="sec"><div class="card">__MANUAL__</div></div>
</main>
<footer>唯讀模擬資料（測試沙箱／yfinance）｜export_snapshot.py 自動生成　<span id="gen"></span></footer>
<script type="application/json" id="DATA">__DATA__</script>
<script>
const D = JSON.parse(document.getElementById('DATA').textContent);
const $ = id => document.getElementById(id);
const fmt = v => (v==null||v===undefined)?'—':Number(v).toLocaleString();
const sgn = v => v>0?'+':'';
const cls = v => (v>0?'pos':v<0?'neg':'muted');
const esc = s => String(s??'—').replace(/&/g,'&').replace(/</g,'<');
const money = v => (v==null)?'—':`<span class="${cls(v)}">${sgn(v)}${fmt(Math.round(v))}</span>`;
function tab(s){
  document.querySelectorAll('nav a').forEach(a=>a.classList.toggle('active',a.dataset.s===s));
  document.querySelectorAll('.sec').forEach(e=>e.classList.toggle('active',e.id===s));
  const R={au:renderAu,tr:renderTr,pf:renderPf,ln:renderLn,ch:renderCh,or:renderOr,bt:renderBt};
  if(R[s]) R[s]();
}
function equitySvg(ts){
  let cum=0; const pts=ts.map(t=>(cum+=t.pnl||0));
  if(pts.length<2) return '<p class="muted">樣本不足</p>';
  const lo=Math.min(...pts,0), hi=Math.max(...pts,0), rng=(hi-lo)||1;
  const W=860,H=180,P=8,pw=W-16,ph=H-34;
  const xy=pts.map((v,i)=>`${(P+i/(pts.length-1)*pw).toFixed(1)},${(P+(hi-v)/rng*ph).toFixed(1)}`).join(' ');
  const y0=P+(hi-0)/rng*ph, last=pts[pts.length-1];
  const col=last>=0?'#2ecc71':'#e74c3c';
  return `<svg viewBox="0 0 ${W} ${H}">
  <line x1="${P}" y1="${y0.toFixed(1)}" x2="${W-P}" y2="${y0.toFixed(1)}" stroke="#3a4150" stroke-dasharray="4 4"/>
  <polyline points="${xy}" fill="none" stroke="${col}" stroke-width="2"/>
  <text x="${P}" y="${H-8}" fill="#8b93a3" font-size="12">累計淨損益 ${sgn(last)}${fmt(Math.round(last))} 元（${pts.length} 筆）</text></svg>`;
}

/* ── 📊 儀表板 ────────────────────────────────── */
function renderOv(){
  const p=D.perf, pf=p.pf==null?'∞':p.pf;
  const per={};
  D.trades.forEach(t=>{const a=per[t.s]||(per[t.s]=[0,0,0]);a[0]++;a[1]+=t.pnl||0;a[2]+=(t.pnl||0)>0?1:0;});
  const symRows=Object.entries(per).sort((a,b)=>b[1][1]-a[1][1]).map(([s,a])=>
    `<tr><td><b>${esc(s)}</b></td><td>${a[0]}</td><td>${(a[2]/a[0]*100).toFixed(0)}%</td><td>${money(a[1])}</td></tr>`).join('');
  const tags={};
  D.trades.forEach(t=>{const a=tags[t.tag]||(tags[t.tag]=[0,0]);a[0]++;a[1]+=t.pnl||0;});
  const tagRows=Object.entries(tags).sort((a,b)=>b[1][1]-a[1][1]).map(([k,a])=>
    `<tr><td>${esc(k)}</td><td>${a[0]}</td><td>${money(a[1])}</td></tr>`).join('');
  $('ov').innerHTML=`
  <div class="grid">
  <div class="card kpi"><div class="l">總交易</div><div class="v">${p.n}</div></div>
  <div class="card kpi"><div class="l">勝率</div><div class="v">${p.win_rate.toFixed(1)}%</div></div>
  <div class="card kpi"><div class="l">總淨損益</div><div class="v ${cls(p.total)}">${sgn(p.total)}${fmt(Math.round(p.total))}</div></div>
  <div class="card kpi"><div class="l">獲利因子</div><div class="v">${pf}</div></div>
  <div class="card kpi"><div class="l">最大回撤</div><div class="v ${cls(D.extra.mdd)}">${sgn(D.extra.mdd)}${fmt(Math.abs(D.extra.mdd))}</div></div>
  </div>
  <div class="card"><h2>📈 淨值曲線</h2>${equitySvg(D.trades)}</div>
  <div class="grid" style="grid-template-columns:1fr 1fr">
  <div class="card"><h2>🧩 標的統計</h2><table><tr><th>標的</th><th>筆數</th><th>勝率</th><th>淨損益</th></tr>${symRows}</table></div>
  <div class="card"><h2>🏷 出場標籤</h2><table><tr><th>標籤</th><th>筆數</th><th>淨損益</th></tr>${tagRows}</table></div>
  </div>`;
  $('gen').textContent=D.meta.generated+'｜資料區間 '+D.meta.range;
}

/* ── 交易表格共用 ─────────────────────────────── */
const TR_HEAD='<tr><th>#</th><th>日期</th><th>標的</th><th>方向</th><th>進場</th><th>出場</th><th>原因</th><th>標籤</th><th>淨損益</th><th>R</th></tr>';
function tradeRows(list){
  return list.map(t=>`<tr>
  <td>${t.id}</td><td>${esc(t.d)}</td><td><b>${esc(t.s)}</b></td><td>${esc(t.dir)}</td>
  <td>${esc(t.et)}@${esc(t.ep)}</td><td>${esc(t.xt)}@${esc(t.xp)}</td>
  <td><span class="badge b-x">${esc(t.xr)}</span></td><td>${esc(t.tag)}</td>
  <td>${money(t.pnl)}</td><td>${t.r==null?'—':sgn(t.r)+t.r}</td></tr>`).join('');
}

/* ── ⚡ 自動執行 ──────────────────────────────── */
function renderAu(){
  let auto=D.runs.filter(r=>r.kind==='live'||r.kind==='paper');
  let fb=false;
  if(!auto.length){ auto=D.runs.slice(0,3); fb=true; }
  const note=fb?'<div class="card muted">ℹ️ 以下為最近回測 run 預覽（交易日 09:15 起每 15 分鐘，Actions 會累積 PAPER-YYYYMMDD 模擬交易）</div>':'';
  if(!auto.length){$('au').innerHTML=note+'<div class="card muted">尚無任何 run</div>';return;}
  const badge=k=>`<span class="badge b-${k}">${k==='live'?'⚡本機引擎':k==='paper'?'🤖模擬重放':'⏱回測'}</span>`;
  $('au').innerHTML=note+`<div class="krow" style="margin-bottom:14px">`+auto.map(r=>
    `<span class="kbd" onclick="auPick('${r.id}')">${badge(r.kind)} ${esc(r.id)}｜${r.n} 筆 ${sgn(Math.round(r.total))}${fmt(Math.round(r.total))}</span>`).join('')+`</div>
  <div id="au-d"></div><div class="card" style="margin-top:12px"><h2>交易明細</h2>
  <div class="tbl-wrap"><table>${TR_HEAD}<tbody id="au-tr"></tbody></table></div></div>`;
  auPick(auto[0].id);
}
function auPick(rid){
  document.querySelectorAll('#au .kbd').forEach(k=>k.classList.remove('on'));
  [...document.querySelectorAll('#au .kbd')].find(k=>k.textContent.includes(rid))?.classList.add('on');
  const rs=D.runs.find(r=>r.id===rid), ts=D.trades.filter(t=>t.rid===rid);
  $('au-d').innerHTML=`<div class="card"><h2>⚡ ${esc(rid)}</h2>
  <div class="grid" style="grid-template-columns:repeat(5,1fr)">
  <div class="kpi"><div class="l">筆數</div><div class="v">${rs.n}</div></div>
  <div class="kpi"><div class="l">勝率</div><div class="v">${rs.win_rate.toFixed(0)}%</div></div>
  <div class="kpi"><div class="l">淨損益</div><div class="v ${cls(rs.total)}">${sgn(rs.total)}${fmt(Math.round(rs.total))}</div></div>
  <div class="kpi"><div class="l">獲利因子</div><div class="v">${rs.pf??'∞'}</div></div>
  <div class="kpi"><div class="l">日期</div><div class="v" style="font-size:13px">${esc(rs.dates)}</div></div>
  </div></div>`;
  $('au-tr').innerHTML=tradeRows(ts);
}

/* ── 📋 交易紀錄 ──────────────────────────────── */
function renderTr(){
  $('tr').innerHTML=`
  <div class="frow">
  <input id="q" placeholder="搜尋標的／日期／標籤…" oninput="trFilter()" size="24">
  <select id="fdir" onchange="trFilter()"><option value="">全部方向</option><option>short</option><option>long</option></select>
  <select id="ftag" onchange="trFilter()"><option value="">全部標籤</option>
  ${[...new Set(D.trades.map(t=>t.tag).filter(Boolean))].map(x=>`<option>${esc(x)}</option>`).join('')}</select>
  <span class="muted" id="trcount"></span></div>
  <div class="tbl-wrap"><table>${TR_HEAD}<tbody id="trbody"></tbody></table></div>`;
  trFilter();
}
function trFilter(){
  const q=($('q').value||'').toLowerCase();
  const dir=$('fdir').value, tag=$('ftag').value;
  const hit=D.trades.filter(t=>
    (!q||JSON.stringify([t.s,t.d,t.tag,t.rid]).toLowerCase().includes(q))
    &&(!dir||t.dir===dir)&&(!tag||t.tag===tag));
  $('trbody').innerHTML=tradeRows(hit);
  $('trcount').textContent=`${hit.length} / ${D.trades.length} 筆`;
}

/* ── 📈 交易績效 ──────────────────────────────── */
function renderPf(){
  const e=D.extra;
  const wdRows=e.weekday.map(w=>`<tr><td>${esc(w.wd)}</td><td>${w.n}</td><td>${w.win}%</td><td>${money(w.net)}</td></tr>`).join('');
  const rbRows=e.r_buckets.map(r=>`<tr><td>${esc(r.bucket)}</td><td>${r.n}</td><td>${money(r.net)}</td></tr>`).join('');
  const hbRows=e.hold.map(h=>`<tr><td>${esc(h.bucket)}</td><td>${h.n}</td><td>${h.win}%</td><td>${money(h.net)}</td></tr>`).join('');
  const maxBar=Math.max(...e.daily.map(d=>Math.abs(d.net)),1);
  const dailyRows=e.daily.map(d=>{
    const w=(Math.abs(d.net)/maxBar*100).toFixed(1);
    const col=d.net>=0?'#2ecc71':'#e74c3c';
    return `<tr><td>${esc(d.date)}</td><td>${d.n}</td><td>${money(d.net)}</td>
    <td style="min-width:120px"><div class="bar"><div style="width:${w}%;background:${col}"></div></div></td></tr>`;}).join('');
  $('pf').innerHTML=`
  <div class="grid">
  <div class="card kpi"><div class="l">最大回撤</div><div class="v neg">${fmt(Math.abs(e.mdd))}</div></div>
  <div class="card kpi"><div class="l">交易日數</div><div class="v">${e.daily.length}</div></div>
  <div class="card kpi"><div class="l">單日最佳</div><div class="v pos">${fmt(Math.max(...e.daily.map(d=>d.net)))}</div></div>
  <div class="card kpi"><div class="l">單日最差</div><div class="v neg">${fmt(Math.min(...e.daily.map(d=>d.net)))}</div></div>
  <div class="card kpi"><div class="l">R>2 佔比</div><div class="v">${((e.r_buckets.filter(r=>r.bucket==='2~5'||r.bucket==='>5').reduce((a,r)=>a+r.n,0)/D.trades.length)*100||0).toFixed(1)}%</div></div>
  </div>
  <div class="card"><h2>📅 每日損益</h2><table><tr><th>日期</th><th>筆數</th><th>淨損益</th><th></th></tr>${dailyRows}</table></div>
  <div class="grid" style="grid-template-columns:1fr 1fr 1fr">
  <div class="card"><h2>📅 星期幾</h2><table><tr><th>星期</th><th>筆數</th><th>勝率</th><th>淨損益</th></tr>${wdRows}</table></div>
  <div class="card"><h2>🎯 R 分佈</h2><table><tr><th>區間</th><th>筆數</th><th>淨損益</th></tr>${rbRows}</table></div>
  <div class="card"><h2>⏱ 持倉時間</h2><table><tr><th>區間</th><th>筆數</th><th>勝率</th><th>淨損益</th></tr>${hbRows}</table></div>
  </div>`;
}

/* ── 🧠 學習迴路 ──────────────────────────────── */
function renderLn(){
  const pt=D.pits;
  let pitHtml;
  if(!pt.found.length){
    pitHtml='<p class="muted">未發現顯著坑點 ✓（繼續累積樣本）</p>';
  }else{
    pitHtml=`<table><tr><th>維度</th><th>分組</th><th>樣本</th><th>虧損率</th><th>avgR</th></tr>
    ${pt.found.map(f=>`<tr><td>${esc(f.dim)}</td><td><b>${esc(f.key)}</b> <span class="badge b-pit">⛔ 坑</span></td>
    <td>${f.n}</td><td class="neg">${f.lr}%</td><td>${sgn(f.avg_r)}${f.avg_r}</td></tr>`).join('')}</table>`;
  }
  const grpRows=pt.groups.map(g=>`<tr style="${g.pit?'background:rgba(231,76,60,.06)':''}">
  <td>${esc(g.dim)}</td><td>${esc(g.key)}</td><td>${g.n}</td>
  <td class="${cls(100-g.lr)}">${g.lr}%</td><td>${sgn(g.avg_r)}${g.avg_r}</td>
  <td>${g.pit?'<span class="badge b-pit">坑</span>':''}</td></tr>`).join('');
  const ruleRows=D.rules.length?D.rules.map(r=>`<tr><td><b>${esc(r.name)}</b></td>
  <td>${r.approved?'<span class="badge b-ok">已核准</span>':'<span class="badge b-bt">未核准</span>'}</td>
  <td class="muted">${esc(r.note)}</td></tr>`).join(''):'<p class="muted">尚無規則（本機：journal.py rules --add）</p>';
  $('ln').innerHTML=`
  <div class="card"><h2>🧠 坑點挖掘（樣本 ${D.meta.n} 筆｜全體虧損率基準 ${pt.base}%）</h2>
  <p class="muted">判定：樣本 ≥4 且 虧損率−基準 ≥20 個百分點 且 avgR<0</p>${pitHtml}</div>
  <div class="card"><h2>📏 避坑規則（引擎自動擋單）</h2><table><tr><th>規則</th><th>狀態</th><th>證據</th></tr>${ruleRows}</table></div>
  <div class="card"><h2>🔬 全維度分組（虧損率）</h2>
  <div class="tbl-wrap"><table><tr><th>維度</th><th>分組</th><th>樣本</th><th>虧損率</th><th>avgR</th><th></th></tr>${grpRows}</table></div></div>`;
}

/* ── 🧲 籌碼選股 ──────────────────────────────── */
function renderCh(){
  if(!D.chips_ok){
    $('ch').innerHTML='<div class="card muted">🧲 籌碼資料尚未推送 — 在本機執行 <code>python tools\\export_chips.py</code> 後再 <code>push_snapshot.bat</code> 即會出現（每日 17:30 daily_update 後資料最新）</div>';
    return;
  }
  fetch('chips.json?t='+Math.floor(Date.now()/3600000)).then(r=>r.json()).then(c=>{
    let html='';
    if(c.watchlist&&c.watchlist.length){
      html+=`<div class="card"><h2>🧲 觀察清單（盤前選股）</h2><table><tr><th>代碼</th><th>名稱</th><th>價</th><th>漲跌</th><th>均量</th><th>備註</th></tr>
      ${c.watchlist.map(w=>`<tr><td><b>${esc(w.code)}</b></td><td>${esc(w.name)}</td><td>${esc(w.price)}</td>
      <td class="${cls(w.chg)}">${sgn(w.chg)}${w.chg}%</td><td>${fmt(w.vol)}</td><td class="muted">${esc(w.note)}</td></tr>`).join('')}</table></div>`;
    }
    if(c.candidates&&c.candidates.length){
      html+=`<div class="card"><h2>💧 隔日沖候選（漲停 ∩ 分點買超）</h2><table><tr><th>代碼</th><th>名稱</th><th>價</th><th>漲跌</th><th>量</th><th>分點買超</th></tr>
      ${c.candidates.map(x=>`<tr><td><b>${esc(x.code)}</b></td><td>${esc(x.name)}</td><td>${esc(x.close)}</td>
      <td class="${cls(x.chg)}">${sgn(x.chg)}${x.chg}%</td><td>${fmt(x.vol)}</td><td class="pos">${fmt(x.buy)}</td></tr>`).join('')}</table></div>`;
    }
    if(c.nextday){
      html+=`<div class="card"><h2>📈 漲停隔日統計</h2><pre>${esc(c.nextday)}</pre></div>`;
    }
    if(!html) html='<div class="card muted">chips.json 無內容</div>';
    $('ch').innerHTML=html+`<p class="muted">資料時間：${esc(c.generated||'—')}</p>`;
  }).catch(()=>{$('ch').innerHTML='<div class="card muted">chips.json 讀取失敗</div>';});
}

/* ── 🧾 下單紀錄 ──────────────────────────────── */
function renderOr(){
  const o=D.orders;
  if(!o.length){$('or').innerHTML='<div class="card muted">尚無送單紀錄（dry 模式只記錄不送單；test 模式送單後顯示於此）</div>';return;}
  $('or').innerHTML=`<div class="tbl-wrap"><table>
  <tr><th>#</th><th>時間</th><th>標的</th><th>方向</th><th>數量</th><th>結果</th><th>訊息</th></tr>
  ${o.map(x=>`<tr><td>${x.id}</td><td>${esc(x.ts)}</td><td><b>${esc(x.sym)}</b></td><td>${esc(x.side)}</td>
  <td>${fmt(x.qty)}</td><td>${x.ok?'<span class="badge b-ok">成功</span>':'<span class="badge b-x">失敗</span>'}</td>
  <td class="muted">${esc(x.msg)}</td></tr>`).join('')}</table></div>`;
}

/* ── ⏱ 回測 ──────────────────────────────────── */
function renderBt(){
  const bts=D.runs.filter(r=>r.kind==='bt');
  if(!bts.length){$('bt').innerHTML='<div class="card muted">尚無回測 run</div>';return;}
  $('bt').innerHTML=`<div class="card"><h2>⏱ 依 run_id 分組</h2>
  <div class="tbl-wrap"><table>
  <tr><th>run_id</th><th>筆數</th><th>勝率</th><th>淨損益</th><th>獲利因子</th><th>平均R</th><th>日期</th></tr>
  ${bts.map(r=>`<tr style="cursor:pointer" onclick="btPick('${r.id}')">
  <td><b>${esc(r.id)}</b></td><td>${r.n}</td><td>${r.win_rate.toFixed(0)}%</td>
  <td>${money(r.total)}</td><td>${r.pf??'∞'}</td><td>${sgn(r.avg_r)}${r.avg_r}</td>
  <td class="muted">${esc(r.dates)}</td></tr>`).join('')}</table></div></div><div id="bt-d"></div>`;
  btPick(bts[0].id);
}
function btPick(rid){
  const ts=D.trades.filter(t=>t.rid===rid);
  $('bt-d').innerHTML=`<div class="card"><h2>📋 ${esc(rid)} 明細（${ts.length} 筆）</h2>
  <div class="tbl-wrap"><table>${TR_HEAD}<tbody>${tradeRows(ts)}</tbody></table></div></div>`;
}

renderOv();
</script></body></html>"""


def main():
    ap = argparse.ArgumentParser(description="產生九分頁唯讀儀表板（Vercel / Pages）")
    ap.add_argument("--out", default=str(BASE / "snapshot"), help="輸出資料夾")
    a = ap.parse_args()

    if not DB.exists():
        print(f"找不到資料庫：{DB}")
        return 1
    conn = tdb.connect()
    tdb.init_db(conn)
    trades = load_trades(conn)
    rules = [dict(r) for r in conn.execute("SELECT * FROM rules ORDER BY id")]
    orders = [dict(r) for r in conn.execute(
        "SELECT * FROM orders ORDER BY id DESC LIMIT 100")]
    pit_base, pits, groups = mine_pits(conn)
    conn.close()

    extra = perf_extra(trades)

    manual_src = BASE / "docs" / "操作手冊.md"
    manual_html = md_to_html(manual_src.read_text(encoding="utf-8")) if manual_src.exists() \
        else "<p>手冊檔案不存在</p>"

    chips_src = BASE / "data" / "chips_export.json"
    chips_ok = chips_src.exists()

    out = Path(a.out)
    out.mkdir(exist_ok=True)
    html_text = build_spa(trades, rules, pits, pit_base, groups,
                          orders, extra, manual_html, chips_ok)
    (out / "index.html").write_text(html_text, encoding="utf-8")
    if chips_src.exists():
        (out / "chips.json").write_text(
            chips_src.read_text(encoding="utf-8"), encoding="utf-8")

    p = perf(trades)
    print(f"✓ 儀表板已生成：{out / 'index.html'}（{len(html_text):,} bytes）")
    print(f"  交易 {p['n']} 筆｜勝率 {p['win_rate']:.1f}%｜淨損益 {p['total']:+,.0f}"
          f"｜坑點 {len(pits)}｜訂單 {len(orders)}｜籌碼 {'✓' if chips_ok else '—'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
