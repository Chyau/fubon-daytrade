# -*- coding: utf-8 -*-
"""export_snapshot.py — 產生唯讀儀表板站（GitHub Pages / Vercel 靜態部署）

從 data/trades.db 讀取交易紀錄，生成「多分頁 SPA」index.html：
  📊 總覽（績效卡片＋淨值曲線＋標的統計＋出場標籤）
  📋 交易紀錄（全部交易，搜尋/篩選）
  ⚡ 自動執行（LIVE-*／PAPER-* 模擬交易，依 run 分組）
  ⏱ 回測報告（依 run_id 分組績效）

設計原則：
  - 純標準庫產出（html/json/datetime），前端零外部相依（無 CDN）
  - 資料以 JSON 內嵌，JS 客端渲染 → 可被 lock_snapshot.py 整頁加密
  - 不含任何憑證／帳號資訊；排除 is_dup=1 與 DEMO 樣本
  - Actions 每次重放後自動重新生成 → Pages/Vercel 自動部署

用法：python tools/export_snapshot.py [--out snapshot] [--recent 30]
"""
import argparse
import json
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
        "n": len(trades), "wins": len(wins),
        "win_rate": len(wins) / len(trades) * 100 if trades else 0,
        "total": sum(nets),
        "pf": round(gp / gl, 2) if gl > 0 else None,
        "avg_r": round(sum(rs) / len(rs), 2) if rs else 0,
    }


def build_spa(trades, rules):
    p = perf(trades)
    dates = sorted({t["date"] for t in trades if t.get("date")})
    gen = datetime.now().strftime("%Y-%m-%d %H:%M")

    # run 分組（回測報告 + 自動執行）
    runs = {}
    for t in trades:
        rid = t.get("run_id") or "—"
        runs.setdefault(rid, []).append(t)
    run_rows = []
    for rid, ts in sorted(runs.items(), key=lambda x: -len(x[1])):
        rp = perf(ts)
        kind = ("live" if rid.startswith("LIVE-")
                else "paper" if rid.startswith("PAPER-") else "bt")
        run_rows.append({"id": rid, "kind": kind, **rp,
                         "dates": f"{min(t['date'] for t in ts)}~{max(t['date'] for t in ts)}" if ts else ""})

    # 交易精簡（避免 JSON 過大）
    slim = [{
        "id": t["id"], "d": t.get("date"), "s": t.get("symbol"),
        "dir": t.get("direction") or "short",
        "et": t.get("entry_time"), "ep": t.get("entry_price"),
        "xt": t.get("exit_time"), "xp": t.get("exit_price"),
        "xr": t.get("exit_reason"), "tag": t.get("result_tag"),
        "pnl": t.get("net_pnl"), "r": (round(t["r_multiple"], 2)
                                       if t.get("r_multiple") is not None else None),
        "rid": t.get("run_id"), "n": t.get("stop_update_count") or 0,
    } for t in trades]

    data = {
        "meta": {"generated": gen, "range": f"{dates[0]} ~ {dates[-1]}" if dates else "",
                 "n": p["n"]},
        "perf": p,
        "runs": run_rows,
        "trades": slim,
        "rules": [{"name": r["name"], "note": (r.get("note") or "")[:80]} for r in rules],
    }

    return PAGE.replace("__DATA__", json.dumps(data, ensure_ascii=False))


PAGE = """<!DOCTYPE html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>⚡ 佛系當沖 — 模擬績效儀表板</title>
<style>
:root{--bg:#0b0e14;--card:#161a22;--bd:#272c37;--tx:#e8eaed;--sub:#8b93a3;
--grn:#2ecc71;--red:#e74c3c;--yel:#f1b143;--acc:#4ea1f3}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--tx);font-size:14px;
font-family:-apple-system,'Segoe UI','PingFang TC','Microsoft JhengHei',sans-serif;-webkit-font-smoothing:antialiased}
header{background:rgba(17,21,29,.92);position:sticky;top:0;z-index:50;border-bottom:1px solid var(--bd);
padding:12px 20px;display:flex;align-items:center;gap:14px;flex-wrap:wrap}
header h1{font-size:16px;font-weight:700;background:linear-gradient(90deg,var(--acc),#7c5cff);
-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
nav{display:flex;gap:4px;flex-wrap:wrap}
nav a{color:var(--sub);text-decoration:none;padding:5px 13px;border-radius:8px;font-size:13px;cursor:pointer;transition:.18s}
nav a:hover{background:#1b2029;color:var(--tx)}
nav a.active{background:linear-gradient(135deg,var(--acc),#7c5cff);color:#fff}
main{padding:18px 20px;max-width:1200px;margin:0 auto}
.sec{display:none}.sec.active{display:block}
.card{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:14px 16px;margin-bottom:14px}
.grid{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:14px}
@media(max-width:760px){.grid{grid-template-columns:repeat(2,1fr)}}
.kpi .l{font-size:11px;color:var(--sub);letter-spacing:.5px;margin-bottom:5px}
.kpi .v{font-size:22px;font-weight:700;font-variant-numeric:tabular-nums}
.pos{color:var(--grn)}.neg{color:var(--red)}
h2{font-size:14px;margin:2px 0 10px;padding-left:10px;border-left:3px solid var(--acc)}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{background:#13161d;color:var(--sub);font-weight:500;padding:7px 9px;text-align:left;
border-bottom:1px solid var(--bd);position:sticky;top:0;z-index:1}
td{padding:6px 9px;border-bottom:1px solid #1e2128}
tr:hover td{background:#1e2330}
.tbl-wrap{max-height:520px;overflow:auto;border:1px solid var(--bd);border-radius:8px;
-webkit-overflow-scrolling:touch}
.muted{color:var(--sub);font-size:12px}
input,select{background:#13161d;color:var(--tx);border:1px solid var(--bd);border-radius:6px;
padding:6px 10px;font-size:13px}
.badge{display:inline-block;padding:1px 7px;border-radius:4px;font-size:11px;font-weight:600}
.b-live{background:rgba(46,204,113,.15);color:var(--grn)}
.b-paper{background:rgba(78,161,243,.15);color:var(--acc)}
.b-bt{background:rgba(139,147,163,.12);color:var(--sub)}
.b-x{background:rgba(231,76,60,.15);color:var(--red)}
footer{text-align:center;color:var(--sub);font-size:11px;padding:22px}
.frow{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:12px;align-items:center}
.krow{display:flex;gap:8px;flex-wrap:wrap}
.kbd{background:#1e2330;border:1px solid var(--bd);border-radius:5px;padding:2px 9px;font-size:12px;cursor:pointer}
.kbd.on{background:var(--acc);color:#fff;border-color:var(--acc)}
svg{width:100%;height:auto;display:block}
</style></head><body>
<header><h1>⚡ 佛系當沖 — 模擬績效</h1>
<nav>
<a class="active" data-s="ov"  onclick="tab('ov')">📊 總覽</a>
<a data-s="tr"  onclick="tab('tr')">📋 交易紀錄</a>
<a data-s="au"  onclick="tab('au')">⚡ 自動執行</a>
<a data-s="bt"  onclick="tab('bt')">⏱ 回測報告</a>
</nav><span class="muted" style="margin-left:auto" id="meta"></span></header>
<main>
<div id="ov" class="sec active"></div>
<div id="tr" class="sec"></div>
<div id="au" class="sec"></div>
<div id="bt" class="sec"></div>
</main>
<footer>唯讀模擬資料（測試沙箱／yfinance）｜由 export_snapshot.py 自動生成　<span id="gen"></span></footer>
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
  if(s==='tr') renderTr();
  if(s==='au') renderAu();
  if(s==='bt') renderBt();
}

/* ── 總覽 ─────────────────────────────────────── */
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
function renderOv(){
  const p=D.perf, pf=p.pf==null?'∞':p.pf;
  const per={};
  D.trades.forEach(t=>{const a=per[t.s]||(per[t.s]=[0,0,0]);a[0]++;a[1]+=t.pnl||0;a[2]+= (t.pnl||0)>0?1:0;});
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
  <div class="card kpi"><div class="l">平均 R</div><div class="v ${cls(p.avg_r)}">${sgn(p.avg_r)}${p.avg_r}</div></div>
  </div>
  <div class="card"><h2>📈 淨值曲線</h2>${equitySvg(D.trades)}</div>
  <div class="grid" style="grid-template-columns:1fr 1fr">
  <div class="card"><h2>🧩 標的統計（依淨損益）</h2><table><tr><th>標的</th><th>筆數</th><th>勝率</th><th>淨損益</th></tr>${symRows}</table></div>
  <div class="card"><h2>🏷 出場標籤</h2><table><tr><th>標籤</th><th>筆數</th><th>淨損益</th></tr>${tagRows}</table></div>
  </div>`;
  $('meta').textContent=`資料區間 ${D.meta.range}`;
  $('gen').textContent=D.meta.generated;
}

/* ── 交易紀錄 ─────────────────────────────────── */
function tradeRows(list){
  return list.map(t=>`<tr>
  <td>${t.id}</td><td>${esc(t.d)}</td><td><b>${esc(t.s)}</b></td><td>${esc(t.dir)}</td>
  <td>${esc(t.et)}@${esc(t.ep)}</td><td>${esc(t.xt)}@${esc(t.xp)}</td>
  <td><span class="badge b-x">${esc(t.xr)}</span></td><td>${esc(t.tag)}</td>
  <td>${money(t.pnl)}</td><td>${t.r==null?'—':sgn(t.r)+t.r}</td></tr>`).join('');
}
const TR_HEAD='<tr><th>#</th><th>日期</th><th>標的</th><th>方向</th><th>進場</th><th>出場</th><th>原因</th><th>標籤</th><th>淨損益</th><th>R</th></tr>';
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

/* ── 自動執行（LIVE + PAPER）──────────────────── */
function renderAu(){
  let auto=D.runs.filter(r=>r.kind==='live'||r.kind==='paper');
  let fallback=false;
  if(!auto.length){
    auto=D.runs.slice(0,3);   // 尚無 LIVE/PAPER → 預覽最近的模擬 run
    fallback=true;
  }
  const note=fallback
    ?'<div class="card muted">ℹ️ 以下為最近回測 run 的預覽（週一盤中 Actions 開跑後，此分頁將改顯示每 15 分鐘累積的模擬交易 PAPER-YYYYMMDD）</div>'
    :'';
  if(!auto.length){$('au').innerHTML=note+'<div class="card muted">尚無任何 run</div>';return;}
  const badge=k=>`<span class="badge b-${k}">${k==='live'?'⚡本機引擎':k==='paper'?'🤖模擬重放':'⏱回測'}</span>`;
  let html=note+`<div class="krow" style="margin-bottom:14px">`+auto.map(r=>
    `<span class="kbd" onclick="auPick('${r.id}')">${badge(r.kind)} ${esc(r.id)}｜${r.n} 筆 ${sgn(Math.round(r.total))}${fmt(Math.round(r.total))}</span>`).join('')+`</div>
  <div id="au-d"></div><div class="card" style="margin-top:12px"><h2>交易明細</h2>
  <div class="tbl-wrap"><table>${TR_HEAD}<tbody id="au-tr"></tbody></table></div></div>`;
  $('au').innerHTML=html;
  auPick(auto[0].id);
}
function auPick(rid){
  document.querySelectorAll('#au .kbd').forEach(k=>k.classList.remove('on'));
  [...document.querySelectorAll('#au .kbd')].find(k=>k.textContent.includes(rid))?.classList.add('on');
  const rs=D.runs.find(r=>r.id===rid), ts=D.trades.filter(t=>t.rid===rid);
  $('au-d').innerHTML=`<div class="card"><h2>⚡ ${esc(rid)} 摘要</h2>
  <div class="grid" style="grid-template-columns:repeat(5,1fr)">
  <div class="kpi"><div class="l">筆數</div><div class="v">${rs.n}</div></div>
  <div class="kpi"><div class="l">勝率</div><div class="v">${rs.win_rate.toFixed(0)}%</div></div>
  <div class="kpi"><div class="l">淨損益</div><div class="v ${cls(rs.total)}">${sgn(rs.total)}${fmt(Math.round(rs.total))}</div></div>
  <div class="kpi"><div class="l">獲利因子</div><div class="v">${rs.pf??'∞'}</div></div>
  <div class="kpi"><div class="l">日期</div><div class="v" style="font-size:13px">${esc(rs.dates)}</div></div>
  </div></div>`;
  $('au-tr').innerHTML=tradeRows(ts);
}

/* ── 回測報告（run_id 分組）───────────────────── */
function renderBt(){
  const bts=D.runs.filter(r=>r.kind==='bt');
  if(!bts.length){$('bt').innerHTML='<div class="card muted">尚無回測 run</div>';return;}
  $('bt').innerHTML=`<div class="card"><h2>⏱ 依 run_id 分組</h2>
  <div class="tbl-wrap"><table>
  <tr><th>run_id</th><th>類型</th><th>筆數</th><th>勝率</th><th>淨損益</th><th>獲利因子</th><th>平均R</th><th>日期</th></tr>
  ${bts.map(r=>`<tr style="cursor:pointer" onclick="btPick('${r.id}')">
  <td><b>${esc(r.id)}</b></td><td>回測</td><td>${r.n}</td>
  <td>${r.win_rate.toFixed(0)}%</td><td>${money(r.total)}</td>
  <td>${r.pf??'∞'}</td><td>${sgn(r.avg_r)}${r.avg_r}</td><td class="muted">${esc(r.dates)}</td></tr>`).join('')}
  </tbody></table></div></div><div id="bt-d"></div>`;
  btPick(bts[0].id);
}
function btPick(rid){
  const ts=D.trades.filter(t=>t.rid===rid);
  $('bt-d').innerHTML=`<div class="card"><h2>📋 ${esc(rid)} 明細（${ts.length} 筆）</h2>
  <div class="tbl-wrap"><table>${TR_HEAD}<tbody>${tradeRows(ts)}</tbody></table></div></div>`;
}

/* ── init ─────────────────────────────────────── */
if(D.rules&&D.rules.length) console.log('規則', D.rules.length, '條（詳見本機 journal.py）');
renderOv();
</script></body></html>"""


def main():
    ap = argparse.ArgumentParser(description="產生唯讀儀表板站（Vercel / Pages 靜態部署）")
    ap.add_argument("--out", default=str(BASE / "snapshot"), help="輸出資料夾")
    ap.add_argument("--recent", type=int, default=30, help="（相容參數，SPA 已含全部）")
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
    html_text = build_spa(trades, rules)
    (out / "index.html").write_text(html_text, encoding="utf-8")

    p = perf(trades)
    print(f"✓ 儀表板已生成：{out / 'index.html'}（{len(html_text):,} bytes）")
    print(f"  交易 {p['n']} 筆｜勝率 {p['win_rate']:.1f}%｜淨損益 {p['total']:+,.0f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
