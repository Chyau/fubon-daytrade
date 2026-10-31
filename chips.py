# -*- coding: utf-8 -*-
"""
chips.py — 籌碼資料整合模組（佛系當沖法・第一階段選股層）
==========================================================

來源 1｜tw-chips-daily 個股籌碼日報（tunglich.github.io/tw-chips-daily）
  stock.html 內嵌 const D（全資料集，約 7MB，本模組快取至 data/chips_stock.html）：
    dates                 最新約 120 個交易日
    stocks[code]          {n:名稱, o/h/l/c/v:價量, f:外資買賣超(張,推測),
                           shd:融券增減(推測), mgd:大戶持股增減(推測), dt:當沖(推測)...}
    geo_today[code]       「最新交易日分點買超」字串，例：元大-竹科:+104、兆豐-新竹:+104
    geomap[code]          分點地理分布（名稱/座標/買超張數）
    whale[code]           大戶持股比例歷史 [日期, %, 增減]
  ※ 欄位語義以官網頁面為準，標「推測」者請勿直接作為交易依據。

來源 2｜chengwaye.com/nextday-performance 漲停隔日表現
  統計事實（2026/10/28 → 10/29 樣本）：漲停股隔日「開盤均報酬 +2.70%」但
  「收盤均報酬僅 +0.24%、收盤勝率 45.3%」→ 開高走低是常態。
  對照逐字稿「隔日沖大戶：今日買進、隔天獲利了結」→ 漲停股隔日盤中賣壓，
  正是佛系當沖（先賣後買）的高機率標的池。

用法：
  python chips.py 2426              # 2426 個股籌碼摘要
  python chips.py --nextday         # 漲停隔日表（含統計）
  python chips.py --candidates      # 今日隔日沖放空候選（昨日漲停∩分點集中買超）
"""
import io
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime

from logutil import ensure_utf8_stdout

ensure_utf8_stdout()

BASE = "https://raw.githubusercontent.com/tunglich/tw-chips-daily/main/stock.html"
NEXTDAY_URL = "https://chengwaye.com/nextday-performance"
SCANNER_URL = "https://abbyyy1720.github.io/daytrade-scanner/data.json"
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "chips_stock.html")
CACHE_TTL = 20 * 3600  # 20 小時內直接用快取（每日 17:00 後更新一次即可）


def fetch_scanner_candidates(log=None):
    """重新抓取當沖掃描器 data.json → 重建 daytrade_candidates_full.csv / hot.txt
    （掃描器網站每日 17:00 後更新；data/ 遺失後可由此重建）"""
    req = urllib.request.Request(SCANNER_URL, headers={"User-Agent": "Mozilla/5.0"})
    data = json.loads(urllib.request.urlopen(req, timeout=30).read().decode("utf-8"))
    stocks = data.get("stocks", [])
    updated = data.get("updated", "未知")
    base = os.path.dirname(os.path.abspath(__file__))

    def _row(s):
        return {"code": s.get("code", ""), "name": s.get("name", ""),
                "badge": s.get("badgeLabel", s.get("badge", "")),
                "score": s.get("score", ""), "price": s.get("price", ""),
                "chg": s.get("chg", ""), "vol": s.get("vol", ""),
                "avgVol": s.get("avgVol", ""), "foreignBuy": s.get("foreignBuy", ""),
                "vr": s.get("vr", ""), "kd": s.get("kdLabel", ""),
                "atr": s.get("atr", ""),
                "market": "上櫃" if s.get("market") == "otc" else "上市",
                "streak": s.get("streakDays", "")}

    hot = sorted([s for s in stocks if s.get("badge") == "hot"],
                 key=lambda s: s.get("score", 0), reverse=True)
    rest = sorted([s for s in stocks if s.get("badge") != "hot"],
                  key=lambda s: s.get("score", 0), reverse=True)
    all_sorted = hot + rest
    cols = list(_row(all_sorted[0]).keys()) if all_sorted else []
    full_csv = os.path.join(base, "data", "daytrade_candidates_full.csv")
    with open(full_csv, "w", encoding="utf-8-sig", newline="") as f:
        if cols:
            f.write(",".join(cols) + "\n")
            for s in all_sorted:
                r = _row(s)
                f.write(",".join(str(r[c]).replace(",", "") for c in cols) + "\n")
    hot_txt = os.path.join(base, "data", "daytrade_candidates_hot.txt")
    with open(hot_txt, "w", encoding="utf-8") as f:
        f.write(f"# 當沖掃描器推薦股 更新時間: {updated} 共 {len(hot)} 支\n")
        for s in hot:
            r = _row(s)
            f.write(f"{r['code']} {r['name']} score={r['score']} ${r['price']} "
                    f"{r['chg']} avgVol={r['avgVol']}\n")
    if log:
        log.info(f"掃描器清單更新（{updated}）：{len(stocks)} 支（推薦 {len(hot)}）")
    return len(stocks), len(hot), updated


def _get(url, timeout=120):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", errors="replace")


# ==================== 來源 1：個股籌碼 ====================
def load_dataset(force=False):
    """下載（或讀快取）stock.html 並解析 const D"""
    if not force and os.path.exists(CACHE) and time.time() - os.path.getmtime(CACHE) < CACHE_TTL:
        html = open(CACHE, encoding="utf-8", errors="replace").read()
    else:
        html = _get(BASE)
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        open(CACHE, "w", encoding="utf-8").write(html)
    start = html.index("{", html.find("const D = "))
    D, _ = json.JSONDecoder().raw_decode(html[start:])
    return D


def parse_geo(s):
    """'元大-竹科:+104、兆豐-新竹:+104' → [(分點, 張數), ...] 依買超排序"""
    out = []
    for part in (s or "").split("、"):
        m = re.match(r"(.+?)\s*[:：]\s*([+-]?\d+)", part.strip())
        if m:
            out.append((m.group(1), int(m.group(2))))
    return sorted(out, key=lambda x: -x[1])


def stock_summary(D, code):
    """單一個股籌碼摘要（最新交易日）"""
    s = D["stocks"].get(code)
    if not s:
        return None
    n = len(D["dates"])
    last = n - 1
    c = s.get("c") or []
    v = s.get("v") or []
    pct = 0.0
    if last > 0 and len(c) > last and c[last - 1] and c[last]:
        pct = (c[last] / c[last - 1] - 1) * 100
    whale = D.get("whale", {}).get(code) or []
    o_arr, h_arr, l_arr = s.get("o") or [], s.get("h") or [], s.get("l") or []
    amps = [(h_arr[i] - l_arr[i]) / c[i] * 100
            for i in range(max(0, last - 4), last + 1)
            if len(o_arr) > i and len(h_arr) > i and len(l_arr) > i and c[i]]
    return {
        "code": code, "name": s.get("n", ""), "date": D["dates"][last],
        "close": c[last] if len(c) > last else None, "chg_pct": round(pct, 2),
        "volume": v[last] if len(v) > last else None,
        "amp5d": round(sum(amps) / len(amps), 2) if amps else None,  # 近5日均振幅%
        "foreign_net": (s.get("f") or [None] * n)[last],       # 外資買賣超（張，推測）
        "margin_short_chg": (s.get("shd") or [None] * n)[last],  # 融券增減（推測）
        "big_holder_chg": (s.get("mgd") or [None] * n)[last],    # 大戶持股增減（推測）
        "whale_pct": whale[-1][1] if whale else None,            # 大戶持股比例 %
        "branches": parse_geo(D.get("geo_today", {}).get(code, "")),
    }


def broker_view(D, broker_key, min_buy=30, top_n=20):
    """券商角度反向查詢（分點K線 App 的券商視角）：
    全市場掃描 geo_today，找出「指定券商分點今日買超 ≥ min_buy 張」的股票
    — 逐字稿：主力習慣在特定券商據點進出，反向追蹤可鎖定當日標的"""
    hits = []
    for code in D["stocks"]:
        info = stock_summary(D, code)
        if not info:
            continue
        for b, v in info["branches"]:
            if v >= min_buy and broker_key in b:
                hits.append({"code": code, "name": info["name"],
                             "branch": b, "buy": v, "close": info["close"],
                             "chg_pct": info["chg_pct"]})
                break
    hits.sort(key=lambda x: -x["buy"])
    return hits[:top_n]


def yesterday_limit_up(D, threshold=9.4):
    """由 D 資料推算最新交易日的漲停股（漲幅 ≥ 9.4% 涵蓋市/櫃）"""
    last = len(D["dates"]) - 1
    out = []
    for code, s in D["stocks"].items():
        c = s.get("c") or []
        if len(c) > last and last >= 1 and c[last - 1] and c[last]:
            if (c[last] / c[last - 1] - 1) * 100 >= threshold:
                out.append(code)
    return out


def daytrade_short_candidates(D, min_branch=50, top_n=15):
    """隔日沖放空候選（逐字稿邏輯：大戶今日集中買超 → 隔日賣壓）
    條件：最新交易日漲停 ＋ 分點買超集中度 ≥ min_branch 張
    """
    lus = set(yesterday_limit_up(D))
    rows = []
    for code in lus:
        info = stock_summary(D, code)
        if not info:
            continue
        top_buy = max([a for _, a in info["branches"] if a > 0], default=0)
        if top_buy >= min_branch:
            rows.append((top_buy, info))
    rows.sort(key=lambda x: -x[0])
    return rows[:top_n]


# ==================== 來源 2：漲停隔日表現 ====================
def _pct_from(cell):
    m = re.search(r"([+\-]?\d+(?:\.\d+)?)%", cell or "")
    return float(m.group(1)) if m else None


def fetch_nextday():
    """解析漲停隔日表 → (統計, 個股明細 list)"""
    html = _get(NEXTDAY_URL, 60)
    tables = re.findall(r"<table[^>]*>.*?</table>", html, re.S)
    best = None
    for t in tables:
        if "代號" in t:
            rows_n = len(re.findall(r"<tr", t))
            if best is None or rows_n > len(re.findall(r"<tr", best)):
                best = t
    recs = []
    if best:
        for r in re.findall(r"<tr[^>]*>(.*?)</tr>", best, re.S):
            cells = [re.sub(r"<[^>]+>", "", c).strip()
                     for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", r, re.S)]
            if len(cells) >= 11 and re.match(r"^\d{3,5}[A-Z]*$", cells[1]):
                recs.append({"market": cells[0], "code": cells[1], "name": cells[2],
                             "limit_price": cells[3], "vol_ratio": cells[4],
                             "open_pct": _pct_from(cells[5]), "avg_pct": _pct_from(cells[6]),
                             "close_pct": _pct_from(cells[7]), "tag": cells[9],
                             "sector": cells[10]})
    # 由個股明細自行計算統計（比抓頁面文案穩）
    n = len(recs)
    stats = {}
    if n:
        for key in ("open_pct", "avg_pct", "close_pct"):
            vals = [r[key] for r in recs if r[key] is not None]
            stats[f"{key}_avg"] = round(sum(vals) / len(vals), 2) if vals else None
            stats[f"{key}_win"] = round(sum(1 for v in vals if v > 0) / len(vals) * 100, 1) if vals else None
        stats["n"] = n
        stats["tags"] = {}
        for r in recs:
            stats["tags"][r["tag"]] = stats["tags"].get(r["tag"], 0) + 1
    return stats, recs


# 逐字稿提及的知名隔日沖券商據點（分點買超 → 隔日賣壓高風險標的）
KNOWN_DAYTRADE_BRANCHES = ("凱基-台北", "凱基-松山", "富邦-建國",
                           "凱基台北", "凱基松山", "富邦建國")


def known_branch_buys(branches):
    """回傳命中知名隔日沖據點的買超清單"""
    hits = []
    for b, v in branches or []:
        if v > 0 and any(kb in b for kb in KNOWN_DAYTRADE_BRANCHES):
            hits.append((b, v))
    return hits


def load_blacklist(min_trades=1):
    """回測虧損黑名單（from data/trades.db；動態計算，DB 不存在時回傳空）"""
    try:
        import sqlite3
        base = os.path.dirname(os.path.abspath(__file__))
        conn = sqlite3.connect(os.path.join(base, "data", "trades.db"))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT symbol, COUNT(*) n, SUM(net_pnl) net FROM trades "
            "WHERE run_id NOT LIKE 'DEMO%' GROUP BY symbol "
            "HAVING SUM(net_pnl) < 0 AND COUNT(*) >= ?", (min_trades,)).fetchall()
        conn.close()
        return {r["symbol"]: {"n": r["n"], "net": r["net"]} for r in rows}
    except Exception:
        return {}


def build_watchlist(D, min_branch=30, top_n=10, min_avg_vol=2000, etf_adds=None,
                    blacklist=None, strong=False):
    """第一階段（盤前準備）：產生「明日觀察清單」檔
    來源：① 隔日沖候選（最新交易日漲停 ∩ 分點集中買超）
          ② 當沖掃描器推薦股（data/daytrade_candidates_hot.txt，存在時）
    盤前歸零原則：清單只含代碼與客觀數據，不預設漲跌方向。
    """
    lines, seen = [], set()
    for top, info in daytrade_short_candidates(D, min_branch=min_branch, top_n=top_n):
        if info["code"] in seen:
            continue
        if min_avg_vol and (info.get("volume") or 0) < min_avg_vol:
            continue  # 資金門檻：日均量不足（三竹技巧：2000 張以上）
        if strong and ((info.get("close") or 0) < 100
                       or (info.get("amp5d") or 0) < 2):
            continue  # 強勢模式：價<100 或 日均振幅<2%
        seen.add(info["code"])
        brs = "、".join(f"{b}:{v:+d}" for b, v in info["branches"][:3])
        kb = known_branch_buys(info["branches"])
        etf_tag = "｜ETF加碼" if etf_adds and info["code"] in etf_adds else ""
        bl = (blacklist or {}).get(info["code"])
        bl_tag = (f"｜🚫 回測虧損-黑名單（{bl['n']} 筆 淨{bl['net']:+,.0f}）" if bl else "")
        tag = "｜⚠ 隔日沖知名據點買超" if kb else ""
        lines.append(f"{info['code']} {info['name']}｜籌碼候選：分點買超top{top}張｜{brs}｜"
                     f"收{info['close']}（{info['chg_pct']:+.1f}%）{tag}{etf_tag}{bl_tag}")
    hot_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "data", "daytrade_candidates_hot.txt")
    if os.path.exists(hot_path):
        for ln in open(hot_path, encoding="utf-8"):
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            code = ln.split()[0]
            m = re.search(r"avgVol=(\d+)", ln)
            if min_avg_vol and m and int(m.group(1)) < min_avg_vol:
                continue  # 資金門檻：日均量不足
            if strong:
                mp = re.search(r"\$(\d+(?:\.\d+)?)", ln)
                if mp and float(mp.group(1)) < 100:
                    continue  # 強勢模式：價<100
                if m and int(m.group(1)) < 2000:
                    continue  # 強勢模式：量<2000 張
            if code not in seen:
                seen.add(code)
                bl = (blacklist or {}).get(code)
                bl_tag = (f"｜🚫 回測虧損-黑名單（{bl['n']} 筆 淨{bl['net']:+,.0f}）" if bl else "")
                lines.append(f"{ln}{bl_tag} ｜來源：當沖掃描器推薦")
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "data", f"watchlist_{datetime.now():%Y%m%d}.txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write(f"# 觀察清單（{datetime.now():%Y-%m-%d %H:%M} 產生）\n"
                f"# 盤前歸零：只看代碼，不看新聞、不預設方向（看的多賺得越少）\n"
                f"# 明日開盤後交由 ⚡自動執行引擎 或手動依四階段 SOP 觀察 5 分K\n")
        for ln in lines:
            f.write(ln + "\n")
    return out, lines


# ==================== CLI ====================
def main():
    import argparse
    ap = argparse.ArgumentParser(description="籌碼資料整合（tw-chips-daily + chengwaye 隔日表）")
    ap.add_argument("code", nargs="?", help="個股代號，如 2426")
    ap.add_argument("--nextday", action="store_true", help="漲停隔日表")
    ap.add_argument("--candidates", action="store_true", help="今日隔日沖放空候選")
    ap.add_argument("--watchlist", action="store_true",
                    help="第一階段：產生明日觀察清單檔（籌碼候選＋掃描器推薦）")
    ap.add_argument("--min-avg-vol", type=int, default=2000,
                    help="資金門檻：日均量（張），預設 2000（逐字稿三竹技巧）")
    ap.add_argument("--strong", action="store_true",
                    help="強勢模式：價≥100＋日均振幅≥2%＋量≥2000 張（獲利批次假說）")
    ap.add_argument("--broker", metavar="關鍵字",
                    help="券商角度反向查詢：該券商分點今日買超的股票（分點K線技巧）")
    ap.add_argument("--min-buy", type=int, default=30, help="券商買超門檻（張）")
    ap.add_argument("--etf-flow", action="store_true",
                    help="族群輪動：ETF 加碼/減碼＋三大法人＋輪動候選（taiwan-etf-tracker）")
    ap.add_argument("--sector", metavar="代碼",
                    help="族群映射：該股票的產業 peers＋上游供應商＋下游客戶（龍頭→小弟）")
    ap.add_argument("--min-branch", type=int, default=50, help="分點買超集中度門檻（張）")
    ap.add_argument("--force", action="store_true", help="強制重新下載籌碼資料")
    a = ap.parse_args()

    if a.nextday:
        stats, recs = fetch_nextday()
        print("========== 漲停隔日表現統計 ==========")
        if stats:
            print(f"樣本 {stats['n']} 檔 ｜ 標籤分布 {stats['tags']}")
            print(f"開盤均報酬 {stats['open_pct_avg']:+.2f}%（勝率 {stats['open_pct_win']}%）")
            print(f"均價均報酬 {stats['avg_pct_avg']:+.2f}%（勝率 {stats['avg_pct_win']}%）")
            print(f"收盤均報酬 {stats['close_pct_avg']:+.2f}%（勝率 {stats['close_pct_win']}%）"
                  f"  ← 開高走低常態，盤中賣壓來源")
        print("\n隔日走弱標籤（開高走低/直接跌）＝佛系當沖放空觀察池：")
        for r in recs:
            if r["tag"] in ("開高走低", "直接跌"):
                print(f"  {r['code']} {r['name']}（{r['market']}｜{r['tag']}｜開 {r['open_pct']:+.1f}% "
                      f"收 {r['close_pct']:+.1f}%｜{r['sector']}）")
        return

    if a.broker:
        D = load_dataset(force=a.force)
        hits = broker_view(D, a.broker, min_buy=a.min_buy)
        print(f"========== 券商反向查詢：'{a.broker}' 分點買超 ≥{a.min_buy} 張 ==========")
        if not hits:
            print("（無符合標的）")
        for h in hits:
            print(f"  {h['code']} {h['name']}｜{h['branch']} +{h['buy']} 張｜"
                  f"收{h['close']}（{h['chg_pct']:+.1f}%）")
        return

    if a.etf_flow:
        import etf_flow as ef
        d2 = ef.load()
        adds, drops = ef.etf_flow(d2)
        print(f"資料日：{d2.get('today_date')}｜追蹤 ETF {len(d2.get('today', {}))} 支")
        print("\n🟢 ETF 加碼 Top 10（機構資金流入）：")
        for code, rec in adds[:10]:
            print(f"  {code} ｜{rec['add']:+.2f}%｜"
                  + "、".join(f"{n}:{dw:+.2f}%" for n, dw in rec["etfs"][:2]))
        print("\n🔴 ETF 減碼 Top 5：")
        for code, rec in drops[:5]:
            print(f"  {code} ｜{rec['add']:+.2f}%")
        print("\n🔄 輪動候選（ETF加碼＋法人買超 評分）：")
        for c in ef.rotation_candidates(d2, 10):
            print(f"  {c['code']} ｜評分 {c['score']:.2f}｜ETF {c['add']:+.2f}%｜"
                  f"法人 {c['inst_total']:+,} 張｜現價 {c['price']}")
        return

    if a.sector:
        import sector as sector_mod
        D_sc = sector_mod.load()
        grp = sector_mod.rotation_group(D_sc, a.sector)
        if not grp:
            print(f"{a.sector} 不在供應鏈資料中（共 {len(D_sc)} 檔）")
            return
        info = grp["info"]
        print(f"========== 族群映射：{info['code']} {info['name']} ==========")
        print(f"產業：{info['ind']}｜地位：{info['pos'] or '—'}")
        print(f"\n同產業 peers（{len(grp['peers'])}）：")
        print("  " + "、".join(f"{m['code']} {m['name']}" for m in grp["peers"][:12] or ["（無）"]))
        print(f"\n上游供應商（{len(grp['upstream'])}，節錄）：")
        for m in grp["upstream"][:8]:
            print(f"  {m['code']} {m['name']}")
        print(f"\n下游客戶（{len(grp['downstream'])}，節錄）：")
        for m in grp["downstream"][:8]:
            print(f"  {m['code']} {m['name']}")
        print("\n（龍頭啟動 → 同產業 peers 與上下游成員 = 資金輪動觀察池）")
        return

    if a.watchlist:
        D = load_dataset(force=a.force)
        bl = load_blacklist()
        etf_adds = set()
        try:
            import etf_flow as ef
            d2 = ef.load()
            adds, _ = ef.etf_flow(d2)
            etf_adds = {c for c, r in adds if r["add"] >= 0.3}  # ETF 加碼 ≥0.3% 才標記
        except Exception:
            pass
        out, lines = build_watchlist(D, min_branch=a.min_branch,
                                     min_avg_vol=a.min_avg_vol, etf_adds=etf_adds,
                                     blacklist=bl, strong=a.strong)
        print(f"✓ 觀察清單已產生（{len(lines)} 檔）：{out}")
        for ln in lines:
            print("  " + ln)
        return

    D = load_dataset(force=a.force)
    if a.candidates:
        rows = daytrade_short_candidates(D, min_branch=a.min_branch)
        bl = load_blacklist()
        print(f"========== 隔日沖放空候選（漲停 ∩ 分點買超 ≥ {a.min_branch} 張） ==========")
        if not rows:
            print("（今日無符合條件者——寧可錯過，不要做錯）")
        for top, info in rows:
            brs = "、".join(f"{b}:{v:+d}" for b, v in info["branches"][:3])
            b2 = bl.get(info["code"])
            bl_tag = (f"｜🚫 回測虧損-黑名單（{b2['n']} 筆 淨{b2['net']:+,.0f}）" if b2 else "")
            print(f"  {info['code']} {info['name']} 收{info['close']} ({info['chg_pct']:+.1f}%)｜"
                  f"分點 top{top} 張｜{brs}{bl_tag}")
        return

    if a.code:
        info = stock_summary(D, a.code)
        if not info:
            print(f"{a.code} 不在資料集中（共 {len(D['stocks'])} 檔）")
            return
        print(f"========== {info['code']} {info['name']} 籌碼摘要（{info['date']}） ==========")
        print(f"收盤 {info['close']}（{info['chg_pct']:+.2f}%）")
        print(f"外資買賣超 {info['foreign_net']} 張｜融券增減 {info['margin_short_chg']} 張｜"
              f"大戶持股增減 {info['big_holder_chg']}｜大戶持股 {info['whale_pct']}%")
        print("分點買超（最新交易日）：")
        for b, v in info["branches"][:8]:
            print(f"  {b:<14}{v:+d} 張")
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
