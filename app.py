# -*- coding: utf-8 -*-
"""
app.py — 佛系當沖量化系統 Web 儀表板（Flask，port 9757）
=========================================================
路由：
  /                儀表板（KPI＋權益曲線＋最近交易＋標籤分布）
  /trades          交易紀錄列表
  /trades/<id>     交易詳情（事件流＋停損軌跡＋進場特徵快照）
  /report          交易績效報告（可 ?run_id= 過濾）
  /journal         學習迴路：坑點挖掘＋規則管理（新增/核准/撤銷）
  /chips           籌碼選股：隔日沖候選＋漲停隔日表＋個股籌碼查詢
  /orders          真實下單紀錄（orders 表，含測試/正式、成功/失敗）
  /backtest        線上執行合成資料回測（可設天數/風險預算/亂數種子）
  /api/stats       JSON 績效摘要
  /api/trades      JSON 交易清單

啟動：python app.py  →  http://127.0.0.1:9757
"""
import json as _json
import os
import sys

from flask import Flask, request, redirect, url_for, render_template_string

from logutil import get_logger, ensure_utf8_stdout

ensure_utf8_stdout()

import db as tdb            # noqa: E402
import strategy             # noqa: E402
import backtest as bt       # noqa: E402
import chips as chips_mod   # noqa: E402
import live as live_mod     # noqa: E402
from journal import DIMS    # noqa: E402

log = get_logger("web")
app = Flask(__name__)

PORT = 9757
HOST = os.environ.get("HOST", "127.0.0.1")  # VPS 部署設 HOST=0.0.0.0（搭配雲端防火牆／Tailscale，勿裸露公網）

# ==================== 樣式與版型 ====================
CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:"Microsoft JhengHei","PingFang TC",system-ui,Consolas,monospace;
     background:radial-gradient(1100px 500px at 85% -10%,#16234a 0%,#0b1220 55%) fixed #0b1220;
     color:#e2e8f0;padding:18px;font-size:14px}
.wrap{max-width:1080px;margin:0 auto}
.nav{position:sticky;top:10px;z-index:50;max-width:1080px;margin:0 auto 18px;display:flex;
     gap:4px;flex-wrap:wrap;padding:9px 11px;background:rgba(13,20,38,.9);
     backdrop-filter:blur(8px);border:1px solid #1e293b;border-radius:14px}
.nav a{padding:7px 13px;border-radius:9px;background:transparent;color:#94a3b8;
       text-decoration:none;font-size:13px;transition:.15s}
.nav a:hover{background:#1e2c4f;color:#e2e8f0}
.nav a.on{background:linear-gradient(135deg,#2563eb,#3b82f6);color:#fff;font-weight:700}
h1{font-size:21px;margin-bottom:16px;color:#f8fafc;letter-spacing:.5px;
   border-bottom:1px solid #1e293b;padding-bottom:10px}
h2{font-size:15px;margin:14px 0 8px;color:#7dd3fc}
.card{background:linear-gradient(180deg,#121c33,#0f1830);border:1px solid #1e293b;
      border-radius:14px;padding:15px 17px;margin-bottom:16px;
      box-shadow:0 4px 18px rgba(0,0,0,.28)}
.card p{margin:5px 0}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(118px,1fr));gap:10px;margin-bottom:16px}
.kpi{background:linear-gradient(180deg,#131f3a,#101a30);border:1px solid #23345c;
     border-radius:12px;padding:13px 8px;text-align:center;transition:.15s}
.kpi:hover{transform:translateY(-2px);border-color:#3b82f6}
.kpi .l{font-size:11px;color:#64748b}
.kpi .v{font-size:17px;font-weight:700;margin-top:4px}
.tw{overflow-x:auto;border-radius:10px}
.tw table{min-width:max-content}
table{width:100%;border-collapse:collapse;font-size:13px}
th{position:sticky;top:0;color:#7dd3fc;text-align:left;padding:8px 9px;
   border-bottom:1px solid #24365f;font-weight:700;white-space:nowrap;
   background:#0f1830;z-index:2}
td{padding:7px 9px;border-bottom:1px solid #16203a;vertical-align:top;white-space:nowrap}
tr:nth-child(even) td{background:rgba(255,255,255,.015)}
tr:hover td{background:#14203b}
a{color:#93c5fd;text-decoration:none}
a:hover{text-decoration:underline}
.muted{color:#64748b;font-size:12px}
.pos{color:#22c55e}.neg{color:#ef4444}
.pit{background:#2a1215;border:1px solid #7f1d1d;border-radius:8px}
.btn{display:inline-block;padding:7px 15px;border-radius:9px;
     background:linear-gradient(135deg,#2563eb,#3b82f6);color:#fff;
     text-decoration:none;font-size:13px;border:0;cursor:pointer;
     transition:.15s;font-weight:600}
.btn:hover{filter:brightness(1.18)}
.btn.gray{background:linear-gradient(135deg,#334155,#475569)}
.btn.red{background:linear-gradient(135deg,#b91c1c,#dc2626)}
.btn.green{background:linear-gradient(135deg,#15803d,#16a34a)}
input,select{background:#16213a;color:#e2e8f0;border:1px solid #334155;
             border-radius:7px;padding:6px 10px;font-size:13px}
input:focus,select:focus{outline:none;border-color:#3b82f6;
                         box-shadow:0 0 0 2px rgba(59,130,246,.25)}
input[type=checkbox]{accent-color:#3b82f6;background:transparent;border:0;
                     padding:0;vertical-align:middle;width:auto}
code{background:#16213a;padding:2px 7px;border-radius:6px;font-size:12px;color:#93c5fd}
.bdg{display:inline-block;padding:2px 9px;border-radius:999px;font-size:11px;
     font-weight:700;white-space:nowrap}
.bdg.g{background:#052e16;color:#4ade80;border:1px solid #14532d}
.bdg.r{background:#2a1215;color:#f87171;border:1px solid #7f1d1d}
.bdg.b{background:#0c1f45;color:#60a5fa;border:1px solid #1e3a8a}
.bdg.o{background:#2a1a05;color:#fbbf24;border:1px solid #78350f}
.bdg.s{background:#1c2432;color:#94a3b8;border:1px solid #334155}
.bdg.p{background:#1e1033;color:#c084fc;border:1px solid #5b21b6}
.manual{line-height:1.75;font-size:14px}
.manual h2{margin-top:18px}.manual h3{margin-top:12px;color:#a5b4fc}
.manual table{margin:8px 0}.manual th,.manual td{border:1px solid #1e293b}
.manual ul,.manual ol{margin:6px 0 6px 22px}
.manual li{margin:3px 0}
@media(max-width:720px){body{padding:10px}.card{padding:12px}
  .nav{position:static}}
"""
BASE = """<!DOCTYPE html><html lang="zh-TW"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
{% if refresh %}<meta http-equiv="refresh" content="{{ refresh }}">{% endif %}
<title>佛系當沖量化系統</title><style>{{ css }}</style></head><body>
<div class="nav">
  <a href="{{ url_for('index') }}">📊 儀表板</a>
  <a href="{{ url_for('live_page') }}">⚡ 自動執行</a>
  <a href="{{ url_for('trades') }}">📋 交易紀錄</a>
  <a href="{{ url_for('report') }}">📈 交易績效</a>
  <a href="{{ url_for('journal_page') }}">🧠 學習迴路</a>
  <a href="{{ url_for('chips_page') }}">🧲 籌碼選股</a>
  <a href="{{ url_for('orders_page') }}">🧾 下單紀錄</a>
  <a href="{{ url_for('backtest_page') }}">⏱ 回測</a>
  <a href="{{ url_for('help_page') }}">📖 手冊</a>
</div><div class="wrap">{{ content|safe }}</div>
<script>
/* 導覽列：目前頁高亮 */
document.querySelectorAll('.nav a').forEach(function(a){
  if (a.getAttribute('href') === location.pathname) a.classList.add('on');
});
/* 寬表格自動包橫向捲軸容器（不切字、不撐破卡片） */
document.querySelectorAll('.card').forEach(function(c){
  var t = c.querySelector('table');
  if (t && t.scrollWidth > c.clientWidth - 6){
    var w = document.createElement('div'); w.className = 'tw';
    t.parentNode.insertBefore(w, t); w.appendChild(t);
  }
});
/* 結果標籤 / 原因碼 / 事件種類 → 徽章上色 */
var K = [['TRAIL','g'],['REVERSAL','r'],['TREND','b'],['GAP','o'],['FLAT','s'],
         ['ENTRY','g'],['EXIT','r'],['ORDER_FAIL','r'],['ORDER','b'],
         ['GATE_BLOCK','r'],['OF_SIGNAL','p'],['AUTO_RULE','b'],
         ['BUDGET_SKIP','o'],['TRIGGER','s'],['STOP_UPDATE','s']];
document.querySelectorAll('table td').forEach(function(td){
  var t = (td.textContent || '').trim();
  if (!/^[RXES]_[A-Z0-9_]+$/.test(t) &&
      !/^(ENTRY|EXIT)$/.test(t)) return;
  var cls = 's';
  for (var i = 0; i < K.length; i++){
    if (t.indexOf(K[i][0]) >= 0){ cls = K[i][1]; break; }
  }
  td.innerHTML = '<span class="bdg ' + cls + '">' + t + '</span>';
});
</script></body></html>"""


def page(tpl, refresh=0, **ctx):
    body = render_template_string(tpl, **ctx)
    return render_template_string(BASE, css=CSS, content=body, refresh=refresh)


def q(sql, args=()):
    conn = tdb.connect()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def money(v):
    try:
        return f"{float(v):+,.0f}"
    except (TypeError, ValueError):
        return "—"


def cls(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "muted"
    return "pos" if f > 0 else ("neg" if f < 0 else "muted")


def fmt(v, spec):
    try:
        return format(float(v), spec)
    except (TypeError, ValueError):
        return "—"


def equity_svg(w=920, h=170):
    rows = q("SELECT date, cum_net_pnl FROM equity_curve ORDER BY id")
    if len(rows) < 2:
        return "<p class='muted'>權益曲線資料不足（先執行回測）</p>"
    vals = [r["cum_net_pnl"] or 0 for r in rows]
    lo, hi = min(vals + [0]), max(vals + [0])
    rng = (hi - lo) or 1
    pts = " ".join(
        f"{10 + i * (w - 20) / (len(vals) - 1):.1f},{10 + (hi - v) / rng * (h - 20):.1f}"
        for i, v in enumerate(vals))
    zero_y = 10 + (hi - 0) / rng * (h - 20)
    color = "#22c55e" if vals[-1] >= 0 else "#ef4444"
    return (f"<svg width='{w}' height='{h}' style='background:#0f172a;border-radius:10px'>"
            f"<line x1='10' y1='{zero_y:.1f}' x2='{w - 10}' y2='{zero_y:.1f}' "
            f"stroke='#334155' stroke-dasharray='4'/>"
            f"<polyline points='{pts}' fill='none' stroke='{color}' stroke-width='2'/></svg>"
            f"<p class='muted'>累計淨損益 {money(vals[-1])} 元（{len(vals)} 筆交易）</p>")


def compute_pits(min_n=4, gap=0.2, where="", params=()):
    rows = q("SELECT * FROM trades WHERE COALESCE(is_dup,0)=0 "
             "AND run_id NOT LIKE 'DEMO%'" + (where or ""), params)
    if not rows:
        return None, [], []
    base = sum(1 for r in rows if (r["net_pnl"] or 0) <= 0) / len(rows)
    items, found = [], []
    for dim, fn in DIMS.items():
        buckets = {}
        for r in rows:
            try:
                buckets.setdefault(fn(r), []).append(r)
            except Exception:
                buckets.setdefault("?", []).append(r)
        for key in sorted(buckets, key=str):
            rs = buckets[key]
            n = len(rs)
            if n < min_n:
                continue
            n_loss = sum(1 for r in rs if (r["net_pnl"] or 0) <= 0)
            lr = n_loss / n
            avg_r = sum(r["r_multiple"] or 0 for r in rs) / n
            pit = (lr - base >= gap and avg_r < 0)
            if pit:
                found.append((dim, key, n, n_loss, lr, avg_r))
            items.append({"dim": dim, "key": str(key), "n": n, "n_loss": n_loss,
                          "lr": lr, "avg_r": avg_r, "pit": pit})
    return base, items, found


# ==================== 篩選工具（各分頁共用） ====================

def _flt():
    """解析 GET 篩選參數（回傳 dict；空值＝不篩）"""
    g = request.args
    try:
        limit = min(2000, max(10, int(g.get("limit", 200) or 200)))
    except ValueError:
        limit = 200
    return {"symbol": (g.get("symbol") or "").strip().upper(),
            "d1": (g.get("d1") or "").strip().replace("/", "-"),
            "d2": (g.get("d2") or "").strip().replace("/", "-"),
            "direction": (g.get("direction") or "").strip(),
            "tag": (g.get("tag") or "").strip(),
            "exit": (g.get("exit") or "").strip().upper(),
            "pnl": (g.get("pnl") or "").strip(),
            "run_id": (g.get("run_id") or "").strip(),
            "side": (g.get("side") or "").strip(),
            "mode": (g.get("mode") or "").strip(),
            "ok": (g.get("ok") or "").strip(),
            "dup": (g.get("dup") or "").strip(),
            "limit": limit}


def _trades_where(f, with_run=True, hide_dup=None):
    """交易篩選 → (WHERE 子句, 參數)。f 來自 _flt()。
    hide_dup：None=依 f["dup"]（預設隱藏 is_dup）；False=不隱藏（run 專屬報告用）"""
    w, p = [], []
    if f.get("symbol"):
        w.append("symbol LIKE ?")
        p.append(f["symbol"] + "%")
    if f.get("d1"):
        w.append("date >= ?")
        p.append(f["d1"])
    if f.get("d2"):
        w.append("date <= ?")
        p.append(f["d2"])
    if f.get("direction") in ("short", "long"):
        w.append("direction = ?")
        p.append(f["direction"])
    if f.get("tag"):
        w.append("result_tag LIKE ?")
        p.append(f["tag"] + "%")
    if f.get("exit"):
        w.append("exit_reason LIKE ?")
        p.append(f["exit"] + "%")
    if f.get("pnl") == "win":
        w.append("net_pnl > 0")
    elif f.get("pnl") == "loss":
        w.append("net_pnl < 0")
    if with_run and f.get("run_id"):
        w.append("run_id LIKE ?")
        p.append(f["run_id"] + "%")
    if hide_dup is None:
        hide_dup = f.get("dup") != "show"
    if hide_dup:
        w.append("COALESCE(is_dup,0)=0")
    return (" AND " + " AND ".join(w)) if w else "", p


def _filter_bar(f, fields, action=""):
    """共通篩選列（GET 表單）。fields：要顯示的欄位名 list"""
    inp = lambda name, ph, size, val: (f'<input name="{name}" value="{val}" '
                                       f'size="{size}" placeholder="{ph}">')
    sel = lambda name, opts, cur: (
        f'<select name="{name}"><option value="">全部</option>' + "".join(
            f'<option value="{v}" {"selected" if v == cur else ""}>{t}</option>'
            for v, t in opts) + "</select>")
    out = ['<div class="card"><form method="get" style="font-size:12px;line-height:2">']
    for k in fields:
        if k == "symbol":
            out.append(f'標的 {inp("symbol", "2330", 7, f["symbol"])}')
        elif k == "d1":
            out.append(f'起 <input type="date" name="d1" value="{f["d1"]}">')
        elif k == "d2":
            out.append(f'訖 <input type="date" name="d2" value="{f["d2"]}">')
        elif k == "direction":
            out.append("方向 " + sel("direction", [("short", "做空"), ("long", "做多")],
                                     f["direction"]))
        elif k == "tag":
            out.append("標籤 " + sel("tag", [
                ("R_TRAIL_LOCK", "移動停利"), ("R_STOP_REVERSAL", "停損反轉"),
                ("R_TREND_RUN", "趨勢行"), ("R_GAP_AWAY", "跳空"),
                ("R_FLAT_CHOP", "盤整")], f["tag"]))
        elif k == "exit":
            out.append(f'出場碼 {inp("exit", "X_STOP…", 9, f["exit"])}')
        elif k == "pnl":
            out.append("盈虧 " + sel("pnl", [("win", "賺"), ("loss", "賠")],
                                     f["pnl"]))
        elif k == "run_id":
            out.append(f'run_id {inp("run_id", "前綴比對", 14, f["run_id"])}')
        elif k == "side":
            out.append("方向 " + sel("side", [("buy", "買"), ("sell", "賣")],
                                     f["side"]))
        elif k == "mode":
            out.append("環境 " + sel("mode", [("dry", "dry"), ("test", "test"),
                                              ("live", "正式⚠")], f["mode"]))
        elif k == "ok":
            out.append("結果 " + sel("ok", [("1", "成功"), ("0", "失敗")],
                                     f["ok"]))
        elif k == "limit":
            out.append(f'筆數 {inp("limit", "200", 5, f["limit"])}')
        elif k == "dup":
            ck = " checked" if f.get("dup") == "show" else ""
            out.append(f'<label><input type="checkbox" name="dup" value="show"{ck}> '
                       f'含重複紀錄</label>')
    out.append('<button class="btn">套用篩選</button> '
               f'<a href="{action or request.path}" class="muted">清除</a></form></div>')
    return "".join(out)


def suggest_for_direction(direction):
    """依方向回傳建議標的 list（多空分類清單）：
    做多→做多+偏多；做空→做空+偏空；多空雙向→四組全部（做空組在前）
    無資料 → []（呼叫端自行後備）"""
    try:
        import longshort as _ls
        if direction == "long":
            _d, rows = _ls.load_longshort(labels=("做多", "偏多"))
            return [r["stock_no"] for r in rows]
        _d, rows = _ls.load_longshort()
        if direction == "both":
            _d2, lrows = _ls.load_longshort(labels=("做多", "偏多"))
            rows = list(rows) + list(lrows)
        return [r["stock_no"] for r in rows]
    except Exception:
        return []


# ==================== 路由 ====================
@app.route("/")
def index():
    rows = q("SELECT * FROM trades ORDER BY id DESC LIMIT 12")
    rep = bt.performance(q("SELECT * FROM trades")) if rows else None
    kpis = []
    if rep:
        kpis = [("交易數", rep["n"], ""),
                ("勝率", fmt(rep["win_rate"], ".1f") + "%", ""),
                ("總淨損益", money(rep["total"]), cls(rep["total"])),
                ("期望值/筆", money(rep["avg"]), cls(rep["avg"])),
                ("獲利因子", "∞" if rep["pf"] == float("inf") else fmt(rep["pf"], ".2f"), ""),
                ("平均R", fmt(rep["avg_r"], ".2f"), cls(rep["avg_r"])),
                ("最大回撤", money(rep["mdd"]), "neg" if rep["mdd"] < 0 else "")]
    tags = list((rep or {}).get("tags", {}).items())
    return page("""<h1>📊 佛系當沖量化系統</h1>
{% if kpis %}<div class="kpis">{% for l,v,c in kpis %}<div class="kpi"><div class="l">{{l}}</div>
<div class="v {{c}}">{{v}}</div></div>{% endfor %}</div>{% endif %}
<div class="card"><h2>累計損益曲線</h2>{{ equity|safe }}</div>
<div class="card"><h2>最近交易</h2>
<table><tr><th>#</th><th>日期</th><th>標的</th><th>進</th><th>出</th><th>原因</th><th>標籤</th><th>淨損益</th><th>R</th></tr>
{% for t in rows %}<tr><td>{{t.id}}</td><td><a href="{{ url_for('trade_detail', tid=t.id) }}">{{t.date}}</a></td>
<td>{{t.symbol}}</td><td>{{t.entry_time}}</td><td>{{t.exit_time}}</td>
<td>{{t.exit_reason}}</td><td>{{t.result_tag}}</td>
<td class="{{cls(t.net_pnl)}}">{{money(t.net_pnl)}}</td><td>{{fmt(t.r_multiple, '.2f')}}</td></tr>{% endfor %}</table>
<p class="muted" style="margin-top:8px">僅顯示 12 筆｜完整清單見「交易紀錄」</p></div>
{% if tags %}<div class="card"><h2>結果標籤分布</h2>
{% for k,v in tags %}<code>{{k}}</code> × {{v}}　{% endfor %}</div>{% endif %}""",
                 kpis=kpis, rows=rows, rep=rep, tags=tags, equity=equity_svg(),
                 money=money, cls=cls, fmt=fmt)


@app.route("/trades")
def trades():
    f = _flt()
    where, params = _trades_where(f)
    rows = q(f"SELECT * FROM trades WHERE 1=1{where} "
             "ORDER BY id DESC LIMIT ?", (*params, f["limit"]))
    hid = 0
    if f["dup"] != "show":
        w2, p2 = _trades_where(f, hide_dup=False)
        hid = q(f"SELECT COUNT(*) c FROM trades WHERE 1=1{w2}", p2)[0]["c"] - len(rows)
    hid_note = f"｜已隱藏重複 {hid} 筆" if hid > 0 else ""
    return page("""<h1>📋 交易紀錄（{{rows|length}} 筆{{hid_note}}）</h1>
{{fbar|safe}}<div class="card">
<table><tr><th>#</th><th>日期/標的</th><th>觸發</th><th>進場</th><th>停損軌跡</th><th>出場</th>
<th>原因/標籤</th><th>淨損益</th><th>R</th></tr>
{% for t in rows %}<tr><td>{{t.id}}</td>
<td><a href="{{ url_for('trade_detail', tid=t.id) }}">{{t.date}} {{t.symbol}}</a></td>
<td>{{t.trigger_time}}@{{t.trigger_open}}</td><td>{{t.entry_time}}@{{t.entry_price}}</td>
<td class="muted">{{t.initial_stop}} → {{t.final_stop}}（{{t.stop_update_count}}）</td>
<td>{{t.exit_time}}@{{t.exit_price}}</td><td>{{t.exit_reason}}<br><span class="muted">{{t.result_tag}}</span></td>
<td class="{{cls(t.net_pnl)}}">{{money(t.net_pnl)}}</td><td>{{fmt(t.r_multiple, '.2f')}}</td></tr>{% endfor %}</table></div>""",
                 rows=rows, fbar=_filter_bar(f, ["symbol", "d1", "d2", "direction",
                                                 "tag", "exit", "pnl", "run_id",
                                                 "dup", "limit"]),
                 hid_note=hid_note,
                 money=money, cls=cls, fmt=fmt)


@app.route("/trades/<int:tid>")
def trade_detail(tid):
    conn = tdb.connect()
    tdb.init_db(conn)
    row = tdb.get_trade(conn, tid)
    if not row:
        conn.close()
        return redirect(url_for("trades"))
    t = dict(row)
    events = [dict(r) for r in tdb.list_events(conn, tid)]
    stops = [dict(r) for r in tdb.list_stop_updates(conn, tid)]
    conn.close()
    try:
        feat = _json.dumps(_json.loads(t["features"]), ensure_ascii=False, indent=2) if t["features"] else "（無）"
    except Exception:
        feat = t["features"] or "（無）"
    return page("""<h1>交易 #{{t.id}}｜{{t.date}} {{t.symbol}}</h1>
<div class="card"><h2>流程</h2>
<p>觸發：{{t.trigger_time}} 長黑K開盤 <b>{{t.trigger_open}}</b></p>
<p>進場：{{t.entry_time}} @ <b>{{t.entry_price}}</b>（{{t.entry_reason}}，{{t.lots}} 張＝{{t.shares}} 股）</p>
<p>停損：{{t.initial_stop}} → {{t.final_stop}}（更新 {{t.stop_update_count}} 次）
{% if stops %}<br>軌跡：{% for s in stops %}{{s.bar_time}}:{{s.new_stop}}　{% endfor %}{% endif %}</p>
<p>出場：{{t.exit_time}} @ <b>{{t.exit_price}}</b>（{{t.exit_reason}} → {{t.result_tag}}）</p>
<p>損益：毛 <span class="{{cls(t.gross_pnl)}}">{{money(t.gross_pnl)}}</span>
− 手續費 {{money(t.fees)}} − 稅 {{money(t.tax)}}
＝ 淨 <b class="{{cls(t.net_pnl)}}">{{money(t.net_pnl)}}</b>（R={{fmt(t.r_multiple, '.2f')}}）</p></div>
<div class="card"><h2>進場特徵快照（學習用）</h2><pre style="white-space:pre-wrap;font-size:12px">{{feat}}</pre></div>
<div class="card"><h2>事件流</h2>
<table><tr><th>時間</th><th>事件</th><th>原因碼</th><th>詳情</th></tr>
{% for e in events %}<tr><td>{{e.ts}}</td><td>{{e.event}}</td><td>{{e.reason}}</td>
<td class="muted" style="font-size:11px">{{e.detail}}</td></tr>{% endfor %}</table></div>""",
                 t=t, events=events, stops=stops, feat=feat, money=money, cls=cls, fmt=fmt)


@app.route("/report")
def report():
    run_id = request.args.get("run_id")
    f = _flt()
    w_extra, p_extra = _trades_where(f, with_run=False)
    runs = q("SELECT run_id, COUNT(*) n, MAX(id) mx FROM trades "
             "GROUP BY run_id ORDER BY mx DESC LIMIT 25")
    if run_id:
        # run 專屬報告：保持該 run 完整（不隱藏 is_dup，除非明確勾選含重複以外…即預設顯示）
        w_extra, p_extra = _trades_where(f, with_run=False, hide_dup=False)
        rows = q(f"SELECT * FROM trades WHERE run_id=?{w_extra} ORDER BY id DESC",
                 (run_id, *p_extra))
    else:
        w_extra, p_extra = _trades_where(f, with_run=False)
        rows = q(f"SELECT * FROM trades WHERE 1=1{w_extra} ORDER BY id DESC LIMIT ?",
                 (*p_extra, f["limit"]))
    rep = bt.performance(rows) if rows else None
    po_s = pf_s = "—"
    e_calc = None
    if rep:
        po_s = "∞" if rep["payoff"] == float("inf") else format(rep["payoff"], ".2f")
        pf_s = "∞" if rep["pf"] == float("inf") else format(rep["pf"], ".2f")
        wr = rep["win_rate"] / 100
        e_calc = wr * rep["avg_win"] + (1 - wr) * rep["avg_loss"]
    return page("""<h1>📈 交易績效{% if run_id %}（run_id={{run_id}}）{% endif %}</h1>
<div class="card"><form method="get" style="font-size:12px">
run_id <select name="run_id">
<option value="">全部彙總（最近 {{f.limit}} 筆）</option>
{% for r in runs %}<option value="{{r.run_id}}" {{'selected' if run_id==r.run_id}}>{{r.run_id}}（{{r.n}} 筆）</option>{% endfor %}
</select>
標的 <input name="symbol" value="{{f.symbol}}" size="7">
起 <input type="date" name="d1" value="{{f.d1}}">
訖 <input type="date" name="d2" value="{{f.d2}}">
標籤 <select name="tag"><option value="">全部</option>
<option value="R_TRAIL_LOCK" {{'selected' if f.tag=='R_TRAIL_LOCK'}}>移動停利</option>
<option value="R_STOP_REVERSAL" {{'selected' if f.tag=='R_STOP_REVERSAL'}}>停損反轉</option>
<option value="R_TREND_RUN" {{'selected' if f.tag=='R_TREND_RUN'}}>趨勢行</option>
<option value="R_GAP_AWAY" {{'selected' if f.tag=='R_GAP_AWAY'}}>跳空</option>
<option value="R_FLAT_CHOP" {{'selected' if f.tag=='R_FLAT_CHOP'}}>盤整</option></select>
盈虧 <select name="pnl"><option value="">全部</option>
<option value="win" {{'selected' if f.pnl=='win'}}>賺</option>
<option value="loss" {{'selected' if f.pnl=='loss'}}>賠</option></select>
{% if not run_id %}<label><input type="checkbox" name="dup" value="show" {{'checked' if f.dup=='show'}}> 含重複紀錄</label>{% endif %}
<button class="btn">套用</button>
<a href="/report" class="muted">清除</a></form></div>
{% if not rep %}<div class="card">
{% if run_id %}<p>本次回測（run_id={{run_id}}）為 <b>0 筆交易</b>——該標的期間沒有出現符合定義的長黑K
（寧可錯過，不要做錯）。可嘗試：換標的／放寬長黑K參數（CLI <code>--min-body-pct</code>）／改用其他資料源。</p>
{% else %}<p class="muted">資料庫尚無交易紀錄，先到「回測」執行一次</p>{% endif %}</div>
{% else %}<div class="card">
<p>樣本 <b>{{rep.n}}</b> 筆 ｜ 勝 {{rep.wins}}／敗 {{rep.losses}} ｜ 勝率 <b>{{fmt(rep.win_rate, '.1f')}}%</b></p>
<p>總淨損益 <b class="{{cls(rep.total)}}">{{money(rep.total)}}</b> 元
（手續費 {{money(-rep.fees)}}｜交易稅 {{money(-rep.tax)}}）</p>
<p>平均每筆 {{money(rep.avg)}}｜平均獲利 {{money(rep.avg_win)}}｜平均虧損 {{money(rep.avg_loss)}}</p>
<p>盈虧比 {{po_s}}｜獲利因子 {{pf_s}}｜平均R {{fmt(rep.avg_r, '.2f')}}</p>
<p>最大連虧 {{rep.max_consec_loss}} 筆｜最大回撤 {{money(rep.mdd)}} 元｜
平均持倉 {{fmt(rep.avg_bars, '.1f')}} 根（{{fmt(rep.avg_bars*5, '.0f')}} 分鐘）</p>
<p>標籤分布：{% for k,v in rep['tags'].items() %}<code>{{k}}</code>×{{v}} {% endfor %}</p></div>
<div class="card"><h2>期望值拆解（每筆淨損益的數學期望）</h2>
<p>E ＝ 勝率 × 平均獲利 − 敗率 × 平均虧損</p>
<p>＝ {{fmt(rep.win_rate, '.1f')}}% × {{money(rep.avg_win)}} − {{fmt(100 - rep.win_rate, '.1f')}}% × {{money(rep.avg_loss)}}
　＝ <b class="{{cls(e_calc)}}">{{money(e_calc)}}</b> 元/筆</p>
<p>平均R {{fmt(rep.avg_r, '.2f')}}（以進場風險為 1R 的期望值）｜
解讀：<b>E > 0 即正期望系統</b>——趨勢跟隨策略勝率偏低，靠高盈虧比（{{po_s}}）長期獲利。</p></div>
<div class="card"><table><tr><th>#</th><th>日期</th><th>標的</th><th>進</th><th>出</th>
<th>原因</th><th>標籤</th><th>淨損益</th><th>R</th></tr>
{% for t in rows %}<tr><td>{{t.id}}</td><td>{{t.date}}</td><td>{{t.symbol}}</td>
<td>{{t.entry_time}}@{{t.entry_price}}</td><td>{{t.exit_time}}@{{t.exit_price}}</td>
<td>{{t.exit_reason}}</td><td>{{t.result_tag}}</td>
<td class="{{cls(t.net_pnl)}}">{{money(t.net_pnl)}}</td><td>{{fmt(t.r_multiple, '.2f')}}</td></tr>{% endfor %}</table></div>
{% endif %}""", run_id=run_id, rep=rep, rows=rows, po_s=po_s, pf_s=pf_s,
                 e_calc=e_calc, f=f, runs=runs, money=money, cls=cls, fmt=fmt)


@app.route("/journal", methods=["GET", "POST"])
def journal_page():
    conn = tdb.connect()
    tdb.init_db(conn)
    if request.method == "POST":
        act = request.form.get("action")
        if act == "approve":
            tdb.approve_rule(conn, int(request.form["rid"]), 1)
            log.info(f"網頁核准規則 #{request.form['rid']}")
        elif act == "revoke":
            tdb.approve_rule(conn, int(request.form["rid"]), 0)
            log.info(f"網頁撤銷規則 #{request.form['rid']}")
        elif act == "add":
            dim = request.form.get("dim", "").strip()
            vals = [v.strip() for v in request.form.get("value", "").split(",") if v.strip()]
            name = request.form.get("name", "").strip()
            if dim and vals:
                rid = tdb.save_rule(conn, name or f"{dim}={','.join(vals)}",
                                    _json.dumps({"dimension": dim, "value": vals},
                                                ensure_ascii=False), note="web")
                log.info(f"網頁建立規則 #{rid}: {name} {dim}={vals}")
        conn.close()
        return redirect(url_for("journal_page"))
    rules = [dict(r) for r in tdb.list_rules(conn)]
    conn.close()
    min_n = int(request.args.get("min_n", 4))
    f = _flt()
    where, params = _trades_where(f, with_run=False)
    base, items, found = compute_pits(min_n=min_n, where=where, params=params)
    return page("""<h1>🧠 學習迴路（記錄 → 挖坑 → 規則 → 引擎擋單）</h1>
{{fbar|safe}}
{% if base is not none %}<div class="card"><h2>坑點挖掘（篩選範圍虧損率 {{fmt(base*100, '.0f')}}%）</h2>
<table><tr><th>維度</th><th>分組</th><th>樣本</th><th>虧損率</th><th>avgR</th><th></th></tr>
{% for i in items %}<tr class="{{'pit' if i.pit}}"><td>{{i.dim}}</td><td>{{i.key}}</td><td>{{i.n}}</td>
<td>{{fmt(i.lr*100, '.0f')}}%（{{i.n_loss}}/{{i.n}}）</td>
<td class="{{cls(i.avg_r)}}">{{fmt(i.avg_r, '.2f')}}</td>
<td>{% if i.pit %}<b class="neg">⛔ 坑</b>{% endif %}</td></tr>{% endfor %}</table>
<p class="muted" style="margin-top:6px">判定：樣本 ≥{{min_n}} 且虧損率高出基準 ≥20 個百分點且 avgR<0。紅底者為坑。</p></div>
{% else %}<div class="card"><p class="muted">尚無交易資料</p></div>{% endif %}
<div class="card"><h2>避坑規則（核准後引擎自動擋下進場）</h2>
{% for r in rules %}<div style="margin:6px 0">
<b>#{{r.id}} {{r.name}}</b> <code>{{r.condition_json}}</code>
{% if r.approved %}<span class="pos">✓ 已核准</span>
<form style="display:inline" method="post"><input type="hidden" name="action" value="revoke">
<input type="hidden" name="rid" value="{{r.id}}"><button class="btn red">撤銷</button></form>
{% else %}<span class="muted">✗ 未核准</span>
<form style="display:inline" method="post"><input type="hidden" name="action" value="approve">
<input type="hidden" name="rid" value="{{r.id}}"><button class="btn green">核准</button></form>{% endif %}
</div>{% endfor %}
<h2>新增規則</h2>
<form method="post"><input type="hidden" name="action" value="add">
名稱 <input name="name" size="20" placeholder="晚盤不追空">
維度 <select name="dim"><option>trigger_bucket</option><option>weekday</option>
<option>symbol</option><option>body_pct_band</option><option>vol_ratio_band</option>
<option>pattern</option></select>
值 <input name="value" size="30" placeholder="11:30-13:00 或 Mon,Tue">
<button class="btn">建立（未核准）</button></form>
<p class="muted">可下維度：trigger_bucket（進場時段）/ weekday / symbol / body_pct_band（實體%）/ vol_ratio_band（量能比）/ pattern（K線型態）</p></div>""",
                 base=base, items=items, found=found, rules=rules, min_n=min_n,
                 fbar=_filter_bar(f, ["symbol", "d1", "d2", "direction", "pnl"]),
                 money=money, cls=cls, fmt=fmt)


@app.route("/chips")
def chips_page():
    view = request.args.get("view", "")
    code = request.args.get("code", "").strip()
    min_branch = int(request.args.get("min_branch", 50))
    ctx = {"view": view, "code": code, "min_branch": min_branch,
           "cands": None, "nd": None, "info": None, "err": None}
    try:
        D = chips_mod.load_dataset()
        if view == "candidates":
            ctx["cands"] = chips_mod.daytrade_short_candidates(D, min_branch=min_branch)
        elif view == "nextday":
            stats, recs = chips_mod.fetch_nextday()
            if stats:
                ctx["nd"] = {
                    "n": stats.get("n", 0), "tags": stats.get("tags", {}),
                    "open": fmt(stats.get("open_pct_avg"), "+.2f"),
                    "open_win": fmt(stats.get("open_pct_win"), ".1f"),
                    "close": fmt(stats.get("close_pct_avg"), "+.2f"),
                    "close_win": fmt(stats.get("close_pct_win"), ".1f"),
                    "weak": [r for r in recs if r["tag"] in ("開高走低", "直接跌")],
                }
        if code:
            ctx["info"] = chips_mod.stock_summary(D, code)
            if not ctx["info"]:
                ctx["err"] = f"{code} 不在資料集中（共 {len(D['stocks'])} 檔）"
    except Exception as e:
        ctx["err"] = f"籌碼資料載入失敗：{e}（首次下載約 7MB，請稍後重試）"
        log.error(f"chips 頁面錯誤：{e}", exc_info=True)
    return page("""<h1>🧲 籌碼選股（tw-chips-daily ＋ chengwaye 漲停隔日）</h1>
<div class="card"><form method="get">
<button class="btn" name="view" value="candidates">隔日沖候選（漲停∩分點買超）</button>
門檻 <input name="min_branch" value="{{min_branch}}" size="4"> 張
<button class="btn gray" name="view" value="nextday">漲停隔日表現</button>
｜ 個股 <input name="code" value="{{code}}" size="8" placeholder="2426">
<button class="btn gray">查詢</button></form></div>
{% if err %}<div class="card pit"><p class="neg">{{err}}</p></div>{% endif %}
{% if cands is not none %}<div class="card"><h2>隔日沖放空候選（{{cands|length}} 檔）</h2>
{% if not cands %}<p class="muted">今日無符合條件者——寧可錯過，不要做錯。</p>{% endif %}
<table><tr><th>代碼</th><th>名稱</th><th>收盤</th><th>漲跌</th><th>分點買超</th></tr>
{% for top,info in cands %}<tr><td>{{info.code}}</td><td>{{info.name}}</td>
<td>{{info.close}}</td><td class="{{cls(info.chg_pct)}}">{{info.chg_pct}}%</td>
<td>{% for b,v in info.branches[:4] %}{{b}}:{{v}}　{% endfor %}</td></tr>{% endfor %}</table></div>{% endif %}
{% if nd %}<div class="card"><h2>漲停隔日表現（{{nd.n}} 檔）</h2>
<p>開盤均 {{nd.open}}%（勝率 {{nd.open_win}}%）→ 收盤均 {{nd.close}}%
（勝率 <b>{{nd.close_win}}%</b>）← 開高走低常態＝盤中賣壓來源</p>
<p>標籤分布：{% for k,v in nd['tags'].items() %}<code>{{k}}</code>×{{v}} {% endfor %}</p>
<table><tr><th>代碼</th><th>名稱</th><th>標籤</th><th>隔日開</th><th>隔日收</th><th>族群</th></tr>
{% for r in nd.weak %}<tr><td>{{r.code}}</td><td>{{r.name}}</td><td class="neg">{{r.tag}}</td>
<td>{{fmt(r.open_pct, '+.1f')}}%</td><td>{{fmt(r.close_pct, '+.1f')}}%</td>
<td>{{r.sector}}</td></tr>{% endfor %}</table></div>{% endif %}
{% if info %}<div class="card"><h2>{{info.code}} {{info.name}}（{{info.date}}）</h2>
<p>收盤 {{info.close}}（{{info.chg_pct}}%）｜外資 {{info.foreign_net}} 張｜
融券增減 {{info.margin_short_chg}}｜大戶持股 {{info.whale_pct}}%</p>
<p>分點買超：{% for b,v in info.branches[:8] %}{{b}}:{{v}}　{% endfor %}
{% if not info.branches %}<span class="muted">（無分點資料）</span>{% endif %}</p></div>{% endif %}""",
                 **ctx, cls=cls, fmt=fmt)


@app.route("/live")
def live_page():
    st = {k: (dict(v) if isinstance(v, dict) else
              list(v) if isinstance(v, list) else v)
          for k, v in live_mod.engine.state.items()}
    syms = (",".join(st["symbols"]) if st["running"] else live_mod.suggest_symbols())
    open_pos = [{"symbol": k, **v} for k, v in st["open_pos"].items()]
    # 事件流篩選（GET：ev_symbol / ev_kind / hide_stop）
    ev_symbol = (request.args.get("ev_symbol") or "").strip().upper()
    ev_kind = (request.args.get("ev_kind") or "").strip().upper()
    hide_stop = bool(request.args.get("hide_stop"))
    evs = list(st["events"])
    if ev_symbol:
        evs = [e for e in evs if ev_symbol in str(e.get("symbol", "")).upper()]
    if ev_kind:
        evs = [e for e in evs if ev_kind in str(e.get("kind", "")).upper()]
    if hide_stop:
        evs = [e for e in evs if e.get("kind") not in ("STOP_UPDATE",)]
    try:
        import longshort as _ls
        _ls_date, _ls_rows = _ls.load_longshort()
        _ls_n_strong = sum(1 for r in _ls_rows if r["bias_label"] == "做空")
        _ls_n_weak = sum(1 for r in _ls_rows if r["bias_label"] == "偏空")
        # 做多清單需另查（預設 labels 只回做空/偏空；直接從空方 rows 過濾永遠為空）
        _ld_date, _ld_rows = _ls.load_longshort(labels=("做多", "偏多"))
        _long_syms = ",".join(r["stock_no"] for r in _ld_rows)
        _short_syms = ",".join(r["stock_no"] for r in _ls_rows)
        _both_syms = ",".join(dict.fromkeys(
            _short_syms.split(",") + _long_syms.split(",")))
    except Exception:
        _ls_date, _ls_n_strong, _ls_n_weak = None, 0, 0
        _long_syms = _short_syms = _both_syms = ""
    return page("""<h1>⚡ 盤中自動執行引擎（佛系當沖法）</h1>
<div class="card"><h2>狀態</h2>
<p>執行中：<b class="{{'pos' if st.running else 'muted'}}">{{'RUNNING' if st.running else 'STOPPED'}}</b>｜
模式 <b>{{st.mode}}</b>（{{'只記錄，不送單' if st.mode=='dry' else '富邦沙箱送單'}}）｜
行情來源 {{st.source}}｜時段限制 {{'開（平日 08:45–13:35）' if st.gate else '關（強制執行）'}}</p>
<p>標的：{{ st.symbols|join(', ') if st.symbols else '（未設定）' }}｜
方向 <b>{{st.direction}}</b>｜風險預算 {{st.risk_budget}} 元｜週期 {{st.interval}} 秒｜
自動風控 {{'🛡 開（樣本≥' + st.auto_min_n|string + '）' if st.auto_risk else '關'}}</p>
<p class="muted">啟動 {{st.started_at or '—'}}｜最後檢查 {{st.last_check or '—'}}｜
已循環 {{st.cycles}} 次｜錯誤：{{st.last_error or '無'}}</p></div>
{% if st.auto_risk %}<div class="card"><h2>🛡 自動風控（依當時數據自動生成；證據消失自動失效）</h2>
<p>目前生效 <b>{{st.auto_rules|length}}</b> 條（門檻：樣本 ≥{{st.auto_min_n}}、
虧損率高出基準 ≥20 個百分點、avgR<0）｜每 30 分鐘自動重挖</p>
{% for r in st.auto_rules %}<div>• <b>{{r.name}}</b>｜{{r.evidence}}</div>{% endfor %}
{% if not st.auto_rules %}<p class="muted">目前無符合門檻的坑點（樣本累積中）</p>{% endif %}</div>{% endif %}
<div class="card"><h2>控制</h2>
{% set _d = st.get('direction') or 'short' %}{% set _m = st.get('mode') or 'dry' %}{% set _s = st.get('source') or 'yf' %}
<form method="post" action="{{ url_for('live_start') }}">
標的 <input name="symbols" value="{{syms}}" size="22" placeholder="2330,2426">
<button type="button" class="btn" onclick="fillSyms('short')">做空清單</button>
<button type="button" class="btn" onclick="fillSyms('long')">做多清單</button>
<button type="button" class="btn" onclick="fillSyms('both')">多空全清單</button>
方向 <label><input type="radio" name="direction" value="short" {{'checked' if _d=='short'}}>做空</label>
<label><input type="radio" name="direction" value="long" {{'checked' if _d=='long'}}>做多</label>
<label><input type="radio" name="direction" value="both" {{'checked' if _d=='both'}}>多空雙向</label>
模式 <select name="mode"><option value="dry" {{'selected' if _m=='dry'}}>dry 只記錄（零風險）</option>
<option value="test" {{'selected' if _m=='test'}}>test 沙箱送單</option></select>
來源 <select name="source"><option value="yf" {{'selected' if _s=='yf'}}>yfinance（延遲15-20分）</option>
<option value="fubon" {{'selected' if _s=='fubon'}}>富邦SDK即時</option></select>
風險預算 <input name="risk_budget" value="{{st.get('risk_budget') or 5000}}" size="6"> 元
週期s <input name="interval" value="{{st.get('interval') or 20}}" size="3">
<label><input type="checkbox" name="gate" {{'checked' if st.get('gate', True)}}> 僅交易時段</label>
<label><input type="checkbox" name="auto_risk" {{'checked' if st.get('auto_risk', True)}}> 🛡 自動風控</label>
門檻n≥ <input name="auto_min_n" value="{{st.get('auto_min_n') or 10}}" size="3">
<button class="btn green">啟動</button></form>
<script>
/* 5 秒自動刷新會重載整頁→未按「啟動」的表單選擇會被清掉。
   解法：任何變更立即存 localStorage；頁面載入時還原。 */
var FSDT_LISTS = {short: "{{_short_syms}}", long: "{{_long_syms}}", both: "{{_both_syms}}"};
function fillSyms(k){
  var sy = document.querySelector('input[name="symbols"]');
  if (sy && FSDT_LISTS[k]){ sy.value = FSDT_LISTS[k];
    sy.dispatchEvent(new Event('change')); }
}
(function(){
  var SEL = 'input[name="symbols"], input[name="direction"], select[name="mode"], select[name="source"], input[name="risk_budget"], input[name="interval"], input[name="auto_min_n"], input[name="gate"], input[name="auto_risk"]';
  function save(){
    try{
      var v = {};
      var sy = document.querySelector('input[name="symbols"]');
      if (sy) v.symbols = sy.value;
      var r = document.querySelector('input[name="direction"]:checked');
      if (r) v.direction = r.value;
      ['mode','source'].forEach(function(n){
        var e = document.querySelector('select[name="'+n+'"]'); if (e) v[n] = e.value;});
      ['risk_budget','interval','auto_min_n'].forEach(function(n){
        var e = document.querySelector('input[name="'+n+'"]'); if (e) v[n] = e.value;});
      ['gate','auto_risk'].forEach(function(n){
        var e = document.querySelector('input[name="'+n+'"]'); if (e) v[n] = e.checked;});
      localStorage.setItem('fsdt_liveform', JSON.stringify(v));
    }catch(err){}
  }
  function restore(){
    try{
      var v = JSON.parse(localStorage.getItem('fsdt_liveform')||'null');
      if (!v) return;
      var sy = document.querySelector('input[name="symbols"]');
      if (sy && v.symbols) sy.value = v.symbols;
      if (v.direction){
        var r = document.querySelector('input[name="direction"][value="'+v.direction+'"]');
        if (r) r.checked = true;
      }
      ['mode','source','risk_budget','interval','auto_min_n'].forEach(function(n){
        var e = document.querySelector('[name="'+n+'"]');
        if (e && v[n] !== undefined && v[n] !== '') e.value = v[n];});
      ['gate','auto_risk'].forEach(function(n){
        var e = document.querySelector('input[name="'+n+'"]');
        if (e && v[n] !== undefined) e.checked = !!v[n];});
    }catch(err){}
  }
  document.addEventListener('change', save);
  window.addEventListener('beforeunload', save);
  document.addEventListener('DOMContentLoaded', restore);
  restore();
})();
</script>
<form method="post" action="{{ url_for('live_stop') }}" style="margin-top:6px">
<button class="btn red">停止引擎</button></form>
<p class="muted">流程：5分K → 長黑K → 下一根開盤放空（test 模式自動送單）→ 移動停損 →
13:20 時間出場 → 買回。出場一次即止。
{% if _ls_date %}建議標的：🧭 多空分類清單（基準日 {{_ls_date}}｜做空 {{_ls_n_strong}} 檔＋偏空 {{_ls_n_weak}} 檔，
做空優先）→ 無資料時後備：盤前清單／籌碼候選／掃描器推薦。
做多清單（方向選「做多」時複製上行）：<b>{{_long_syms}}</b>。{% else %}建議標的：{{syms}}（籌碼候選／掃描器推薦）。{% endif %}
⚠ 重啟 app 後引擎停止，需重新啟動。</p></div>
{% if open_pos %}<div class="card"><h2>持倉監控（未平倉）</h2>
<table><tr><th>標的</th><th>進場</th><th>進價</th><th>當前停損</th><th>張數</th><th>現價</th><th>浮動損益</th></tr>
{% for p in open_pos %}<tr><td><b>{{p.symbol}}</b></td><td>{{p.entry_time}}</td>
<td>{{p.entry}}</td><td>{{p.stop}}</td><td>{{p.lots}}</td><td>{{p.last}}</td>
<td class="{{cls(p.unreal)}}">{{money(p.unreal)}}</td></tr>{% endfor %}</table></div>{% endif %}
{% if st.of_on %}<div class="card"><h2>📊 內外盤觀察（逐筆即時；僅記錄訊號，不自動下單）</h2>
{% if not st.of_signals %}<p class="muted">尚無訊號——視窗需累積 {{'80+'}} 筆逐筆成交（盤中才有效）</p>{% endif %}
<table><tr><th>時間</th><th>標的</th><th>訊號</th><th>明細</th></tr>
{% for s in st.of_signals[:12] %}<tr><td>{{s.time}}</td><td><b>{{s.symbol}}</b></td>
<td><b>{{s.kind}}</b></td><td class="muted" style="font-size:11px">{{s.detail}}</td></tr>{% endfor %}</table>
<p class="muted">TREND=順勢｜DIVERGE_BUY=內盤高但價漲（承接）｜DIVERGE_SELL=外盤高但價跌（出貨）</p></div>
{% else %}<p class="muted">內外盤觀察未啟用（需富邦 SDK＋憑證登入；重啟 app 自動偵測）</p>{% endif %}
<div class="card"><h2>即時事件流（篩選後 {{evs|length}} 筆，顯示最新 30）</h2>
<form method="get" style="font-size:12px;margin:4px 0">
標的 <input name="ev_symbol" value="{{ev_symbol}}" size="6">
事件 <select name="ev_kind"><option value="">全部</option>
<option value="ENTRY" {{'selected' if ev_kind=='ENTRY'}}>ENTRY 進場</option>
<option value="EXIT" {{'selected' if ev_kind=='EXIT'}}>EXIT 出場</option>
<option value="ORDER" {{'selected' if ev_kind=='ORDER'}}>ORDER 下單</option>
<option value="GATE_BLOCK" {{'selected' if ev_kind=='GATE_BLOCK'}}>GATE_BLOCK 擋單</option>
<option value="OF_SIGNAL" {{'selected' if ev_kind=='OF_SIGNAL'}}>OF_SIGNAL 內外盤</option>
<option value="BUDGET_SKIP" {{'selected' if ev_kind=='BUDGET_SKIP'}}>BUDGET_SKIP 超預算</option>
<option value="TRIGGER_NO_ENTRY" {{'selected' if ev_kind=='TRIGGER_NO_ENTRY'}}>TRIGGER_NO_ENTRY 無觸發</option>
<option value="AUTO_RULE" {{'selected' if ev_kind=='AUTO_RULE'}}>AUTO_RULE 風控</option></select>
<label><input type="checkbox" name="hide_stop" value="1" {{'checked' if hide_stop}}> 隱藏 STOP_UPDATE</label>
<button class="btn">套用</button>
<a href="/live" class="muted">清除</a></form>
{% if not evs %}<p class="muted">（無符合條件的事件）</p>{% endif %}
<table><tr><th>時間</th><th>標的</th><th>事件</th><th>原因碼</th><th>詳情</th></tr>
{% for e in evs[:30] %}<tr><td>{{e.time}}</td><td>{{e.symbol}}</td>
<td>{{e.kind}}</td><td>{{e.reason}}</td>
<td class="muted" style="font-size:11px">{{e.detail}}</td></tr>{% endfor %}</table>
<p class="muted">本頁每 5 秒自動刷新</p></div>""",
                 st=st, syms=syms, open_pos=open_pos, refresh=5, evs=evs,
                 ev_symbol=ev_symbol, ev_kind=ev_kind, hide_stop=hide_stop,
                 money=money, cls=cls, fmt=fmt,
                 _ls_date=_ls_date, _ls_n_strong=_ls_n_strong, _ls_n_weak=_ls_n_weak,
                 _long_syms=_long_syms, _short_syms=_short_syms, _both_syms=_both_syms)


@app.route("/live/start", methods=["POST"])
def live_start():
    mode = request.form.get("mode", "dry")
    source = request.form.get("source", "yf")
    direction = request.form.get("direction", "short")
    symbols = [s.strip() for s in
               request.form.get("symbols", "").replace("，", ",").split(",") if s.strip()]
    if not symbols:
        # 標的留空 → 依方向自動填入多空分類清單（做多→做多+偏多；做空→做空+偏空；雙向→全部）
        symbols = suggest_for_direction(direction)
        if symbols:
            log.info(f"標的留空→依方向 {direction} 自動填入 {len(symbols)} 檔"
                     f"（{','.join(symbols[:6])}…）")
    risk = float(request.form.get("risk_budget", 0) or 0)
    interval = int(request.form.get("interval", 20) or 20)
    gate = bool(request.form.get("gate"))
    auto_risk = bool(request.form.get("auto_risk"))
    auto_min_n = int(request.form.get("auto_min_n", 10) or 10)
    ok, msg = live_mod.engine.start(symbols, mode=mode, source=source,
                                    risk_budget=risk, lots=1,
                                    interval=interval, gate=gate,
                                    auto_risk=auto_risk, auto_min_n=auto_min_n,
                                    direction=direction)
    log.info(f"網頁啟動自動執行：{msg}｜symbols={symbols} mode={mode} source={source} "
             f"方向={direction} 自動風控={auto_risk}(n≥{auto_min_n})")
    return redirect(url_for("live_page"))


@app.route("/live/stop", methods=["POST"])
def live_stop():
    ok, msg = live_mod.engine.stop()
    log.info(f"網頁停止自動執行：{msg}")
    return redirect(url_for("live_page"))


@app.route("/orders")
def orders_page():
    f = _flt()
    w, p = [], []
    if f["symbol"]:
        w.append("symbol LIKE ?")
        p.append(f["symbol"] + "%")
    if f["side"] in ("buy", "sell"):
        w.append("side = ?")
        p.append(f["side"])
    if f["mode"] in ("dry", "test", "live"):
        w.append("mode = ?")
        p.append(f["mode"])
    if f["ok"] in ("0", "1"):
        w.append("is_success = ?")
        p.append(int(f["ok"]))
    where = (" AND " + " AND ".join(w)) if w else ""
    rows = q(f"SELECT * FROM orders WHERE 1=1{where} "
             "ORDER BY id DESC LIMIT ?", (*p, f["limit"]))
    return page("""<h1>🧾 真實下單紀錄（{{rows|length}} 筆）</h1>
{{fbar|safe}}<div class="card">
{% if not rows %}<p class="muted">尚無下單紀錄（fubon_orders.py --send 送出後會記錄於此）</p>{% endif %}
<table><tr><th>#</th><th>時間</th><th>環境</th><th>標的</th><th>方向</th><th>類型</th>
<th>數量</th><th>價格</th><th>委託號</th><th>結果</th><th>訊息</th></tr>
{% for o in rows %}<tr><td>{{o.id}}</td><td>{{o.ts}}</td>
<td>{{'正式⚠' if o.mode=='live' else '測試'}}</td><td>{{o.symbol}}</td><td>{{o.side}}</td>
<td>{{o.order_type}}</td><td>{{o.quantity}}</td><td>{{o.price or '市價'}}</td>
<td>{{o.order_no or '—'}}</td>
<td class="{{'pos' if o.is_success else 'neg'}}">{{'成功' if o.is_success else '失敗'}}</td>
<td class="muted" style="font-size:11px">{{o.message}}</td></tr>{% endfor %}</table></div>""",
              rows=rows,
              fbar=_filter_bar(f, ["symbol", "side", "mode", "ok", "limit"]))


@app.route("/backtest", methods=["GET", "POST"])
def backtest_page():
    err = None
    if request.method == "POST":
        import time as _t
        import uuid as _uuid
        src = request.form.get("src", "fubon")
        days = max(3, min(60, int(request.form.get("days", 15) or 15)))
        seed = int(request.form.get("seed", 42) or 42)
        risk = float(request.form.get("risk_budget", 0) or 0)
        period = request.form.get("period", "5d")
        days_back = max(1, min(180, int(request.form.get("days_back", 60) or 60)))
        use_rules = bool(request.form.get("use_rules", "on"))
        entry_mode = request.form.get("entry_mode", "next_open")
        if entry_mode not in ("next_open", "breakout", "three_short", "three_long",
                              "b12_long", "nextday_long"):
            entry_mode = "next_open"
        direction = request.form.get("direction", "auto")
        if direction not in ("short", "long"):
            direction = None          # auto：依進場模式自動映射
        daytrade_check = bool(request.form.get("daytrade_check"))
        raw = (request.form.get("symbols") or "").replace("，", ",").replace(" ", ",")
        symbols = [s.strip().upper() for s in raw.split(",") if s.strip()][:30]
        cfg = {**strategy.DEFAULTS, "risk_budget": risk,
               "entry_mode": entry_mode, "direction": direction}
        run_id = _t.strftime("%Y%m%d-%H%M%S") + "-" + _uuid.uuid4().hex[:6]
        done, failed, errs, total_bars = [], [], [], 0
        # 當沖/融券資格預檢（僅富邦來源；blocked → 跳過該標的）
        if daytrade_check and src == "fubon" and symbols:
            _dm = {"three_short": "short", "three_long": "long",
                   "b12_long": "long", "nextday_long": "long"}
            _chk_dir = direction or _dm.get(entry_mode, "short")
            try:
                import fubon_orders as fo_mod
                _sdk, _acc = fo_mod.connect_sdk(fo_mod.fubon_cfg())
                _kept = []
                for _s in symbols:
                    _v, _r, _gd = fo_mod.entry_gate(_sdk, _acc, _s, _chk_dir)
                    if _v == "blocked":
                        log.warning(f"⚠ 回測預檢：{_s} 不符合{_chk_dir}當沖資格"
                                    f"（{_r}）→ 跳過")
                        errs.append(f"{_s}: 資格不符({_r})")
                    else:
                        _kept.append(_s)
                symbols = _kept
            except Exception as _e:
                log.warning(f"資格預檢失敗（全部照跑）：{str(_e)[:100]}")
        for sym in symbols:
            try:
                if src == "fubon":
                    import fubon_orders as fo_mod
                    bars = fo_mod.fetch_fubon_history(sym, days_back=days_back, log=log)
                elif src == "yf":
                    bars = bt.fetch_yf(sym, period, log)
                    sym = sym.split(".")[0]
                else:
                    bars = bt.make_demo_bars(days, seed=seed + len(done), log=log)
                    sym = sym or "DEMO"
            except Exception as e:
                bars = None
                errs.append(f"{sym}: {str(e)[:80]}")
            if not bars:
                failed.append(sym)
                continue
            bt.run_backtest(bars, cfg, sym, run_id, log, use_rules=use_rules)
            done.append(sym)
            total_bars += len(bars)
        if not done:
            err = ("所有標的皆無資料：" + ("；".join(errs) if errs else "、".join(failed))
                   if (failed or errs) else "（未輸入標的）")
        else:
            if failed:
                log.warning(f"回測略過無資料標的：{','.join(failed)}")
            log.info(f"網頁回測完成：src={src} {len(done)} 檔（{','.join(done)}）｜"
                     f"總 {total_bars} 根｜run_id={run_id}")
            return redirect(url_for("report", run_id=run_id))
    import longshort as _ls
    _ls_date, _ls_syms = _ls.short_symbols()
    return page("""<h1>⏱ 線上回測</h1>
{% if err %}<div class="card pit"><p class="neg">{{err}}</p></div>{% endif %}
<div class="card">
<form method="post">
<p>資料來源 <select name="src">
<option value="fubon">真實資料（富邦 SDK・測試環境）</option>
<option value="yf">真實資料（yfinance 5 分K）</option>
<option value="demo">合成資料（亂數走勢，管線驗證）</option></select>｜
標的 <input name="symbols" size="72" value="{{ls_syms}}"
 placeholder="2330,2426,2454（逗號分隔多檔，可自行增刪）"></p>
<p>進場模式 <select name="entry_mode">
<option value="next_open">next_open：長K後下一根開盤（原始模式）</option>
<option value="breakout">breakout：突破觸發K高低點（逐字稿爆量）</option>
<option value="three_short">three_short：進場三要素（做空）</option>
<option value="three_long">three_long：進場三要素（做多）</option>
<option value="b12_long">b12_long：B1/B2（做多）</option>
<option value="nextday_long">nextday_long：隔日沖買進（做多跨日）</option></select>｜
方向 <select name="direction">
<option value="auto">自動（依進場模式）</option>
<option value="short">做空</option>
<option value="long">做多</option></select>｜
單筆風險預算 <input name="risk_budget" value="0" size="8"> 元（0＝固定 1 張）｜
<label><input type="checkbox" name="use_rules" checked> 套用核准規則</label>｜
<label><input type="checkbox" name="daytrade_check" checked> 當沖/融券資格預檢</label></p>
{% if ls_syms %}<p class="muted">🧭 已預填多空分類清單（基準日 {{ls_date}}｜
做空＋偏空，做空優先排序）— 來源：權證小哥 tw_stock.db，可自行增刪後執行。</p>{% endif %}
<p>富邦模式：回溯天數 <input name="days_back" value="10" size="3">（需 .env 測試憑證）｜
yfinance 期數 <input name="period" value="5d" size="5">（5 分K 最長 60d）｜
合成模式：交易日數 <input name="days" value="15" size="4">（3–60）｜
種子 <input name="seed" value="42" size="6"></p>
<p class="muted" style="margin:8px 0">⚠ <b>合成資料是固定種子的亂數 K 棒</b>（標的代碼僅為標籤，
各標的結果相同），用途是驗證引擎與學習迴路。<b>評估策略真實績效請選「富邦 SDK」或「yfinance」</b>。
資料覆蓋度：富邦測試環境依模擬行情而定（可能僅最新交易日）、yfinance 台股 5 分K可能稀疏。
CLI：<code>python backtest.py --fubon 2330</code>／<code>--yf 2426.TW</code>／<code>--csv 檔案</code></p>
<button class="btn">執行回測</button></form></div>""",
             err=err, ls_syms=_ls_syms, ls_date=_ls_date)


@app.route("/api/stats")
def api_stats():
    rows = q("SELECT * FROM trades")
    rep = bt.performance(rows) if rows else None
    if rep:
        rep = {k: v for k, v in rep.items() if k != "tags"}
    return {"count": len(rows), "performance": rep}


@app.route("/api/trades")
def api_trades():
    return {"trades": q("SELECT * FROM trades ORDER BY id DESC LIMIT ?",
                        (int(request.args.get("limit", 50)),))}


@app.route("/help")
def help_page():
    import os
    import html as _h
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "docs", "操作手冊.md")
    try:
        md_src = open(path, encoding="utf-8").read()
    except Exception:
        md_src = "# 手冊遺失\n請確認 docs/操作手冊.md 存在。"
    try:
        import markdown as _md
        body = _md.markdown(md_src, extensions=["tables", "fenced_code"])
    except ImportError:
        body = f"<pre style='white-space:pre-wrap;font-size:13px'>{_h.escape(md_src)}</pre>"
    return page("<h1>📖 網頁版操作手冊</h1><div class='card manual'>" + body + "</div>")


if __name__ == "__main__":
    # 自癒：確保 data/ 基本結構與檔案存在（目錄遺失自動重建）
    try:
        import subprocess
        base_dir = os.path.dirname(os.path.abspath(__file__))
        data_dir = os.path.join(base_dir, "data")
        os.makedirs(data_dir, exist_ok=True)
        _c = tdb.connect()   # schema 自癒
        _c.close()
        if not os.path.exists(os.path.join(data_dir, "2426_case_0818.csv")):
            subprocess.run([sys.executable, os.path.join(base_dir, "tools", "gen_2426_case.py")],
                           capture_output=True, text=True)
            log.info("自癒：已重建 2426 案例 CSV")
        if not os.path.exists(os.path.join(data_dir, "daytrade_candidates_hot.txt")):
            try:
                import chips as _chips
                _chips.fetch_scanner_candidates(log=log)
                log.info("自癒：已重建掃描器清單")
            except Exception as _e:
                log.warning(f"自癒：掃描器清單重建略過（{_e}）")
    except Exception as _e:
        log.warning(f"啟動自癒略過：{_e}")
    log.info(f"啟動 Web 儀表板：http://127.0.0.1:{PORT}")
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
