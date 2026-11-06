# -*- coding: utf-8 -*-
"""AI 每日複盤 — 收盤後自動生成白話複盤（學習迴路的人話版）

資料源（全部唯讀查詢 data/trades.db）：
  ① 當日交易（排除 is_dup/DEMO）＋ 模式拆解（entry_mode × 損益）
  ② 生效中的避坑規則 ＋ 當日被規則擋掉的進場（RULE_BLOCK）
  ③ 坑點挖掘快照（pits 指令的資料）
  ④ 送單失敗紀錄（orders 表）
輸出：
  - 白話複盤（LLM）→ 存 ai_reviews 表 → 儀表板 /ai 頁顯示
  - --send 同步推 Telegram（notify）

用法：
  python ai_review.py                    ＃ 生成並顯示今日複盤
  python ai_review.py --date 2026-11-06  ＃ 指定日期
  python ai_review.py --send             ＃ 生成＋推 Telegram
  python ai_review.py --suggest-rules    ＃ AI 讀坑點→建議新避坑規則（僅建議，需人工核准）

LLM 未設定（LLM_API_KEY 空）→ 印出提示後結束，不影響任何流程。
"""
import argparse
import io
import json
import os
import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from logutil import get_logger, ensure_utf8_stdout

ensure_utf8_stdout()
log = get_logger("ai_review", "INFO")

import db as tdb
import llm
import notify


# ---------------- 資料蒐集（唯讀） ----------------

def _conn():
    conn = tdb.connect()
    return conn


def collect(date=None):
    """蒐集當日複盤素材（全部 dict/str，可直接 json.dumps）"""
    conn = _conn()
    if not date:
        row = conn.execute("SELECT MAX(date) FROM trades").fetchone()
        date = row[0] if row and row[0] else None
    if not date:
        conn.close()
        return None, "資料庫尚無交易"

    d = {"date": date}

    # ① 當日交易（排除重複/合成）
    trades = [dict(r) for r in conn.execute(
        "SELECT symbol, direction, entry_time, entry_price, exit_time, exit_price, "
        "exit_reason, result_tag, net_pnl, r_multiple, features FROM trades "
        "WHERE date=? AND COALESCE(is_dup,0)=0 AND run_id NOT LIKE 'DEMO%' "
        "ORDER BY entry_time", (date,)).fetchall()]
    for t in trades:
        try:
            f = json.loads(t.pop("features") or "{}")
            t["mode"] = f.get("entry_mode") or "?"
            t["prev_chg"] = f.get("prev_day_chg")
        except Exception:
            t["mode"] = "?"
    d["trades"] = trades
    d["n"] = len(trades)
    d["pnl"] = round(sum(t["net_pnl"] or 0 for t in trades), 0)
    d["wins"] = sum(1 for t in trades if (t["net_pnl"] or 0) > 0)

    # 模式拆解
    by_mode = defaultdict(lambda: [0, 0.0])
    for t in trades:
        by_mode[t["mode"]][0] += 1
        by_mode[t["mode"]][1] += t["net_pnl"] or 0
    d["by_mode"] = {k: {"n": v[0], "pnl": round(v[1], 0)} for k, v in by_mode.items()}

    # ② 生效中規則
    d["rules"] = [dict(r) for r in conn.execute(
        "SELECT name, condition_json, note FROM rules WHERE approved=1").fetchall()]

    # ③ 當日規則擋單（signals_log）
    d["rule_blocks"] = [dict(r) for r in conn.execute(
        "SELECT symbol, ts, reason, detail FROM signals_log "
        "WHERE date=? AND event='RULE_BLOCK' ORDER BY ts", (date,)).fetchall()]

    # ④ 送單失敗
    d["order_fails"] = [dict(r) for r in conn.execute(
        "SELECT symbol, side, order_type, message FROM orders "
        "WHERE ts LIKE ? AND is_success=0 ORDER BY id LIMIT 20",
        (f"{date}%",)).fetchall()]

    # ⑤ 坑點快照（今日之前的累積統計）
    pits = [dict(r) for r in conn.execute(
        "SELECT dimension, bucket, n_trades, n_loss, loss_rate, avg_r FROM pits "
        "ORDER BY id DESC LIMIT 12").fetchall()]
    d["pits_recent"] = pits

    conn.close()
    return d, None


SYSTEM_PROMPT = """你是台股當沖交易系統的複盤教練。輸入是系統自動蒐集的當日交易數據
（交易明細、模式拆解、生效規則、被擋進場、送單失敗、坑點統計）。
請用繁體中文寫一份精簡的每日複盤，結構：
【今日概況】1-2 句（筆數/勝率/淨損益）
【模式表現】各進場模式盈虧一句話
【虧損歸因】最傷的 1-3 筆交易：發生什麼、為什麼（從 exit_reason/result_tag/進場時段推）
【規則與擋單】生效規則擋了什麼、值不值
【明日注意】1-3 條具體可執行的事項
總長 ≤350 字，語氣專業直接，不廣告不雞湯，數據沒有的就不要編。"""


def build_messages(d):
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(d, ensure_ascii=False, default=str)},
    ]


def save_review(date, kind, content):
    conn = _conn()
    try:
        conn.execute("INSERT INTO ai_reviews(created_at, date, kind, content) "
                     "VALUES (datetime('now','localtime'), ?, ?, ?)",
                     (date, kind, content))
        conn.commit()
    finally:
        conn.close()


def latest_review(kind="daily"):
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT created_at, date, content FROM ai_reviews WHERE kind=? "
            "ORDER BY id DESC LIMIT 1", (kind,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ---------------- 指令 ----------------

def cmd_review(date=None, send=False):
    if not llm.enabled():
        print("✗ LLM 未設定：請在 .env 填 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL")
        print("  （agnes-ai / Gemini OpenAI 端點 / 任何 OpenAI 相容服務皆可）")
        return
    d, err = collect(date)
    if err:
        print(f"✗ {err}")
        return
    print(f"素材：{d['n']} 筆交易｜淨損益 {d['pnl']:+,.0f}｜勝 {d['wins']}")
    text, err = llm.chat(build_messages(d))
    if err:
        print(f"✗ 生成失敗：{err}")
        return
    print("\n" + "=" * 60 + "\n" + text + "\n" + "=" * 60)
    save_review(d["date"], "daily", text)
    print("（已存 ai_reviews 表）")
    if send and notify.configured():
        notify.send(f"【AI 複盤 {d['date']}】\n{text}")
        print("（已推 Telegram）")
    elif send:
        print("（Telegram 未設定，略過推送）")


RULE_PROMPT = """你是台股當沖避坑規則設計師。輸入是坑點統計（維度、分桶、樣本、虧損率、avgR）
與已生效的規則清單。請提出「尚未存在、統計上站得住」的新避坑規則。
限制：
- 維度只能用：trigger_bucket / weekday / symbol / body_pct_band / vol_ratio_band /
  pattern / entry_mode / t_direction / prev_chg_band（進場前可得）
- 樣本 n≥10、虧損率明顯高於基準、avgR<0 才提
- 已有規則不要重複
輸出 JSON：{"suggestions": [{"name": "規則名", "dimension": "...", "value": ["..."],
"evidence": "一句話統計證據"}]}，最多 3 條，沒有就空陣列。"""


def cmd_suggest_rules():
    if not llm.enabled():
        print("✗ LLM 未設定")
        return
    conn = _conn()
    pits = [dict(r) for r in conn.execute(
        "SELECT dimension, bucket, n_trades, n_loss, loss_rate, avg_r FROM pits "
        "ORDER BY id DESC LIMIT 40").fetchall()]
    rules = [dict(r) for r in conn.execute(
        "SELECT name, condition_json FROM rules WHERE approved=1").fetchall()]
    conn.close()
    if not pits:
        print("（尚無坑點統計 — 先跑 journal.py pits）")
        return
    text, err = llm.chat(
        [{"role": "system", "content": RULE_PROMPT},
         {"role": "user", "content": json.dumps(
             {"pits": pits, "active_rules": rules}, ensure_ascii=False, default=str)}],
        json_mode=True, max_tokens=800)
    if err:
        print(f"✗ 生成失敗：{err}")
        return
    try:
        data = json.loads(text)
        sugg = data.get("suggestions", [])
    except Exception:
        print(f"✗ AI 輸出非 JSON：\n{text[:500]}")
        return
    if not sugg:
        print("AI 認為目前沒有值得新增的規則 ✓")
        return
    print("AI 建議的新避坑規則（僅建議 — 核准才生效）：")
    for s in sugg:
        cond = json.dumps({"dimension": s.get("dimension"),
                           "value": s.get("value")}, ensure_ascii=False)
        print(f"  - {s.get('name')}｜{cond}")
        print(f"    證據：{s.get('evidence')}")
        print(f"    建立指令：python journal.py rules --add '{cond}' --name \"{s.get('name')}\"")
    save_review((pits[0].get("bucket") or ""), "rule_suggest", text)


def main():
    ap = argparse.ArgumentParser(description="AI 每日複盤")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD（預設最新交易日）")
    ap.add_argument("--send", action="store_true", help="推 Telegram")
    ap.add_argument("--suggest-rules", action="store_true",
                    help="AI 讀坑點→建議新避坑規則")
    a = ap.parse_args()
    if a.suggest_rules:
        cmd_suggest_rules()
    else:
        cmd_review(a.date, a.send)


if __name__ == "__main__":
    main()
