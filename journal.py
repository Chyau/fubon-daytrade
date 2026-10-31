# -*- coding: utf-8 -*-
"""
journal.py — 交易日誌與避坑學習迴路
====================================
學習閉環（避免採同樣的坑）：
  ① 記錄：每筆交易的事件流＋進出場原因碼＋進場當下特徵快照（features JSON）
  ② 挖掘：pits — 依多維度分組，找「虧損率顯著高於全體基準」的情境
  ③ 建議：產生避坑規則（rules 表，預設未核准，人工把關）
  ④ 套用：--approve 核准後，回測／實單引擎自動擋下符合條件的進場（E_RULE_BLOCK）
  ⑤ 再記錄 → 驗證規則是否有效 → 循環

子命令：
  python journal.py list --limit 20      # 最近交易
  python journal.py show 3               # 單筆詳情（事件流＋停損軌跡＋特徵快照）
  python journal.py pits --min-n 3       # 坑點挖掘（--save 存入 DB）
  python journal.py report [--run-id X]  # 績效摘要
  python journal.py rules --list
  python journal.py rules --add '{"dimension":"trigger_bucket","value":["11:30-13:00"],"name":"晚盤不追空"}' --note "樣本不足"
  python journal.py rules --approve 1 ｜ --revoke 1
"""
import argparse
import json

from logutil import get_logger, ensure_utf8_stdout

ensure_utf8_stdout()

import db as tdb       # noqa: E402
import strategy        # noqa: E402


def _feat(t):
    try:
        return json.loads(t["features"]) if t["features"] else {}
    except Exception:
        return {}


DIMS = {
    "trigger_bucket": lambda t: _feat(t).get("trigger_bucket") or "?",
    "body_pct_band": lambda t: strategy.body_pct_band(_feat(t).get("body_pct", 0.0)),
    "vol_ratio_band": lambda t: strategy.vol_ratio_band(_feat(t).get("vol_ratio")),
    "pattern": lambda t: _feat(t).get("pattern") or "無資料（舊紀錄）",
    "weekday": lambda t: _feat(t).get("weekday") or "?",
    "symbol": lambda t: t["symbol"],
    "exit_reason": lambda t: t["exit_reason"] or "?",
    "result_tag": lambda t: t["result_tag"] or "?",
    "lots": lambda t: str(t["lots"] or 1),
    "price_band": lambda t: ("<30" if (t["entry_price"] or 0) < 30
                             else ("30-100" if (t["entry_price"] or 0) <= 100 else ">100")),
}


def cmd_list(conn, limit):
    rows = tdb.list_trades(conn, limit)
    print(f"最近 {len(rows)} 筆交易：")
    print(f"{'#':<5}{'日期':<11}{'標的':<7}{'進':>6}{'出':>6}{'出場原因':<14}{'標籤':<16}{'淨損益':>10}")
    for r in rows:
        print(f"{r['id']:<5}{r['date']:<11}{r['symbol']:<7}{r['entry_time']:>6}{r['exit_time'] or '':>6}"
              f"{(r['exit_reason'] or ''):<14}{(r['result_tag'] or ''):<16}{r['net_pnl']:>10,.0f}")


def cmd_show(conn, tid):
    r = tdb.get_trade(conn, tid)
    if not r:
        print(f"找不到交易 #{tid}")
        return
    print("=" * 72)
    print(f"交易 #{r['id']}｜{r['date']} {r['symbol']}｜run_id={r['run_id']}")
    print(f"  觸發：{r['trigger_time']} 長黑K開盤 {r['trigger_open']}")
    print(f"  進場：{r['entry_time']} @ {r['entry_price']}（{r['entry_reason']}，{r['lots']} 張）")
    print(f"  停損：初始 {r['initial_stop']} → 最終 {r['final_stop']}（更新 {r['stop_update_count']} 次）")
    trail = " → ".join(f"{u['bar_time']}:{u['new_stop']}"
                       for u in tdb.list_stop_updates(conn, tid))
    print(f"  軌跡：{trail or '（無更新）'}")
    print(f"  出場：{r['exit_time']} @ {r['exit_price']}（{r['exit_reason']} → {r['result_tag']}）")
    print(f"  損益：毛 {r['gross_pnl']:+,.0f} − 手續費 {r['fees']:,.0f} − 稅 {r['tax']:,.0f} "
          f"＝ 淨 {r['net_pnl']:+,.0f}（R={r['r_multiple']}）")
    try:
        feat = json.dumps(json.loads(r["features"]), ensure_ascii=False, indent=2) if r["features"] else "（無）"
    except Exception:
        feat = r["features"]
    print(f"  進場特徵快照：\n{feat}")
    print("  事件流：")
    for ev in tdb.list_events(conn, tid):
        print(f"    {ev['ts']} [{ev['event']}] {ev['reason']}  {(ev['detail'] or '')[:110]}")


def cmd_pits(conn, min_n, gap, save, log):
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM trades WHERE COALESCE(is_dup,0)=0 "
        "AND run_id NOT LIKE 'DEMO%'").fetchall()]
    if not rows:
        print("（資料庫尚無交易紀錄；先跑 python backtest.py --demo）")
        return
    n_total = len(rows)
    base = sum(1 for r in rows if (r["net_pnl"] or 0) <= 0) / n_total
    print(f"樣本 {n_total} 筆 ｜ 全體虧損率 {base:.1%} ｜ "
          f"坑點判定：樣本 ≥{min_n} 且 虧損率−基準 ≥ {gap:.0%} 且 avgR<0")
    print("=" * 76)
    found = []
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
            mark = "  ⛔ 坑" if (lr - base >= gap and avg_r < 0) else ""
            if mark:
                found.append((dim, key, n, n_loss, lr, avg_r))
            print(f"  {dim:<16}{str(key):<18} n={n:<4} 虧損率 {lr:.0%}（{n_loss}/{n}）"
                  f" avgR={avg_r:+.2f}{mark}")
        print("-" * 76)
    if not found:
        print("未發現顯著坑點 ✓（繼續累積樣本）")
        return
    print("⚠ 避坑建議（人工評估後可用 rules --add 建立規則）：")
    for dim, key, n, n_loss, lr, avg_r in found:
        sug = (f"避開 {dim}={key} 的進場（樣本 {n} 筆，虧損率 {lr:.0%} "
               f"vs 全體 {base:.0%}，avgR {avg_r:+.2f}）")
        print(f"  - {sug}")
        if save:
            pid = tdb.save_pit(conn, dim, str(key), n, n_loss, round(lr, 4),
                               round(base, 4), round(avg_r, 3), "pit", sug)
            log.debug(f"坑點 #{pid} 入庫")


def cmd_report(conn, run_id):
    if run_id:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM trades WHERE run_id=?", (run_id,)).fetchall()]
    else:
        # 全體統計：排除重複標記與合成樣本（單一 run_id 檢視則顯示全部）
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM trades WHERE COALESCE(is_dup,0)=0 "
            "AND run_id NOT LIKE 'DEMO%'").fetchall()]
    if not rows:
        print("（無交易紀錄）")
        return
    from backtest import performance, print_report
    symbols = {r["symbol"] for r in rows}
    rep = performance(rows)
    print_report(rep, rows, run_id or "ALL", rows[0]["symbol"] if len(symbols) == 1 else f"{len(symbols)} 標的")


def cmd_rules(conn, add, add_dim, add_value, name, from_pit, approve, revoke, note, log,
              delete=None, auto_generate=False, auto_min_n=10, auto_gap=0.2,
              approve_auto=False):
    if delete:
        tdb.delete_rule(conn, delete)
        print(f"規則 #{delete} 已刪除")
    if auto_generate:
        found = mine_auto_rules(conn, min_n=auto_min_n, gap=auto_gap)
        if not found:
            print(f"無符合門檻的坑點（樣本 ≥{auto_min_n}、虧損率−基準 ≥{auto_gap:.0%}、avgR<0）")
        for r in found:
            exist = tdb.find_rule_by_condition(conn, r["condition_json"])
            if exist:
                print(f"  已存在：#{exist['id']} {exist['name']}（{r['evidence']}）")
                continue
            rid = tdb.save_rule(conn, r["name"], r["condition_json"],
                                source="auto", note=f"證據：{r['evidence']}")
            if approve_auto:
                tdb.approve_rule(conn, rid, 1)
            st = "已核准" if approve_auto else "未核准（人工審查後 --approve）"
            print(f"  ✓ 規則 #{rid} 已生成（{st}）：{r['name']}｜{r['evidence']}")
    cond = None
    if from_pit:  # 從坑點建立規則（證據鏈：pits 表 → rules.note）
        pit = tdb.get_pit(conn, from_pit)
        if not pit:
            print(f"找不到坑點 #{from_pit}（先用 `pits --save` 將挖掘結果入庫）")
            return
        ok_dims = ("trigger_bucket", "weekday", "symbol", "body_pct_band",
                   "vol_ratio_band", "pattern")
        if pit["dimension"] not in ok_dims:
            print(f"⛔ 坑點 #{from_pit} 的維度「{pit['dimension']}」屬結果歸因（進場前不可得），"
                  f"不可作為進場規則。\n   可用維度：{','.join(ok_dims)}")
            return
        cond = {"dimension": pit["dimension"], "value": [pit["bucket"]]}
    elif add_dim:  # 友善格式（避開 shell 引號問題）
        cond = {"dimension": add_dim,
                "value": [v.strip() for v in (add_value or "").split(",") if v.strip()]}
    elif add:
        try:
            cond = json.loads(add)
        except Exception as e:
            print(f"JSON 解析失敗：{e}")
            return
    if cond is not None:
        cond_json = json.dumps({k: v for k, v in cond.items() if k != "name"},
                               ensure_ascii=False)
        exist = tdb.find_rule_by_condition(conn, cond_json)
        if exist:
            print(f"規則已存在：#{exist['id']} {exist['name']}（未重複建立；"
                  f"核准用 --approve {exist['id']}，刪除用 --delete {exist['id']}）")
        else:
            rule_name = cond.get("name") or name or \
                f"{cond.get('dimension')}={','.join(map(str, cond.get('value', [])))}"
            if from_pit:
                note = (f"證據：n={pit['n_trades']} 虧損率 {pit['loss_rate']:.0%} "
                        f"vs 基準 {pit['baseline_loss_rate']:.0%}｜avgR={pit['avg_r']:+.2f}")
            else:
                note = note or ""
            rid = tdb.save_rule(conn, rule_name, cond_json,
                                source="pit_mining" if from_pit else "manual", note=note)
            src = f"（來源：坑點 #{from_pit}）" if from_pit else ""
            print(f"規則 #{rid} 已建立（未核准）{src}：{rule_name}")
            if from_pit:
                print(f"   {note}")
    if approve:
        tdb.approve_rule(conn, approve, 1)
        print(f"規則 #{approve} 已核准 → 引擎自動擋下符合條件的進場（E_RULE_BLOCK）")
    if revoke:
        tdb.approve_rule(conn, revoke, 0)
        print(f"規則 #{revoke} 已撤銷")
    rows = tdb.list_rules(conn)
    if rows:
        print(f"\n{'#':<5}{'核准':<5}名稱 / 條件")
        for r in rows:
            print(f"{r['id']:<5}{'✓' if r['approved'] else '✗':<5}{r['name']}  {r['condition_json']}"
                  f"{('  # ' + r['note']) if r['note'] else ''}")


def mine_auto_rules(conn, min_n=10, gap=0.2):
    """⚡自動風控：從累積交易數據挖掘避坑規則（僅進場端維度＋統計門檻）
    回傳 [{"name","condition_json","evidence"}] — 供 live 引擎即時套用。
    設計：規則只在數據支持時存在（重挖後證據消失即自動失效）＝自我修正。"""
    # 學習統計排除重複標記（is_dup=1）與合成樣本（DEMO）
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM trades WHERE COALESCE(is_dup,0)=0 "
        "AND run_id NOT LIKE 'DEMO%'").fetchall()]
    if len(rows) < min_n:
        return []
    base = sum(1 for r in rows if (r["net_pnl"] or 0) <= 0) / len(rows)
    entry_dims = ("trigger_bucket", "weekday", "symbol", "body_pct_band",
                  "vol_ratio_band", "pattern")
    rules = []
    for dim in entry_dims:
        fn = DIMS.get(dim)
        if not fn:
            continue
        buckets = {}
        for r in rows:
            try:
                buckets.setdefault(fn(r), []).append(r)
            except Exception:
                continue
        for key, rs in buckets.items():
            n = len(rs)
            if n < min_n:
                continue
            n_loss = sum(1 for r in rs if (r["net_pnl"] or 0) <= 0)
            lr = n_loss / n
            avg_r = sum(r["r_multiple"] or 0 for r in rs) / n
            if lr - base >= gap and avg_r < 0:
                rules.append({
                    "name": f"[自動]{dim}={key}",
                    "condition_json": json.dumps({"dimension": dim, "value": [str(key)]},
                                                 ensure_ascii=False),
                    "evidence": f"n={n} 虧損率 {lr:.0%} vs 基準 {base:.0%}｜avgR {avg_r:+.2f}"})
    return rules


def main():
    ap = argparse.ArgumentParser(description="交易日誌與避坑學習迴路")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--log-level", default=None, choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list", parents=[common])
    p.add_argument("--limit", type=int, default=20)
    p = sub.add_parser("show", parents=[common])
    p.add_argument("id", type=int)
    p = sub.add_parser("pits", parents=[common])
    p.add_argument("--min-n", type=int, default=3)
    p.add_argument("--gap", type=float, default=0.2, help="虧損率超出基準的門檻（0.2＝20 個百分點）")
    p.add_argument("--save", action="store_true", help="坑點存入 DB")
    p = sub.add_parser("report", parents=[common])
    p.add_argument("--run-id")
    p = sub.add_parser("rules", parents=[common])
    p.add_argument("--add", metavar="JSON", help="完整條件 JSON（與 --add-dim 擇一）")
    p.add_argument("--add-dim", metavar="DIM", help="維度：trigger_bucket/weekday/symbol/body_pct_band")
    p.add_argument("--add-value", metavar="V1,V2", help="命中即擋下（逗號分隔）")
    p.add_argument("--name", help="規則名稱")
    p.add_argument("--from-pit", type=int, metavar="PIT_ID",
                   help="從坑點建立規則（自動攜帶證據備註；限進場端維度）")
    p.add_argument("--approve", type=int, metavar="ID")
    p.add_argument("--revoke", type=int, metavar="ID")
    p.add_argument("--delete", type=int, metavar="ID", help="刪除規則（含重複項清理）")
    p.add_argument("--note")
    a = ap.parse_args()
    log = get_logger("journal", a.log_level)
    conn = tdb.connect()
    tdb.init_db(conn)
    if a.cmd == "list":
        cmd_list(conn, a.limit)
    elif a.cmd == "show":
        cmd_show(conn, a.id)
    elif a.cmd == "pits":
        cmd_pits(conn, a.min_n, a.gap, a.save, log)
    elif a.cmd == "report":
        cmd_report(conn, a.run_id)
    elif a.cmd == "rules":
        cmd_rules(conn, a.add, a.add_dim, a.add_value, a.name, a.from_pit,
                  a.approve, a.revoke, a.note, log, a.delete)
    conn.close()


if __name__ == "__main__":
    main()
