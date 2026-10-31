# -*- coding: utf-8 -*-
"""
sector.py — 族群輪動映射（逐字稿技巧④：龍頭先漲，小弟跟漲）
=============================================================
資料源：tw-chips-daily supplychain.html（5,501 檔股票的產業別＋上下游關係）
  ind 產業別｜pos 產業地位｜up 上游（供應商）｜dn 下游（客戶，含權重%）

用途：
  1. 同族群成員：給定一檔股票 → 同產業 peers
  2. 上下游鏈：給定龍頭 → 上游供應商＋下游客戶（族群輪動的「小弟」）
  3. 快取：12MB 解析結果存 data/supplychain_D.json
"""
import json
import os
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
URL = "https://raw.githubusercontent.com/tunglich/tw-chips-daily/main/supplychain.html"
CACHE = os.path.join(BASE, "data", "supplychain_D.json")


def load(force=False):
    """載入供應鏈映射（12MB HTML → 解析 const D → dict；結果快取）"""
    if not force and os.path.exists(CACHE):
        return json.load(open(CACHE, encoding="utf-8"))
    req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=180) as r:
        html = r.read().decode("utf-8", errors="replace")
    start = html.index("{", html.find("const D = "))
    d, _ = json.JSONDecoder().raw_decode(html[start:])
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    json.dump(d, open(CACHE, "w", encoding="utf-8"), ensure_ascii=False)
    return d


def sector_info(D, code):
    """單一股票的產業與供應鏈資訊"""
    s = D.get(code)
    if not s:
        return None
    return {"code": code, "name": s.get("n", ""), "ind": s.get("ind", ""),
            "pos": s.get("pos", ""), "prod": s.get("prod", ""),
            "upstream": s.get("up") or [], "downstream": s.get("dn") or []}


def sector_peers(D, code, max_n=12):
    """同產業成員（族群「小弟」候選）"""
    info = sector_info(D, code)
    if not info:
        return []
    ind = info["ind"]
    peers = [c for c, s in D.items()
             if c != code and s.get("ind") == ind]
    return peers[:max_n]


def chain_names(rows, kind="dn", limit=8):
    """解析上下游清單（兩種列格式：dn=[排序,名稱,代碼,...]、up=[名稱,代碼,地區,...]）"""
    out = []
    for r in rows[:limit * 3]:
        if not isinstance(r, list) or len(r) < 2:
            continue
        if kind == "up":
            name, code = str(r[0]), str(r[1]) if len(r) > 1 else ""
        else:
            name = str(r[1]) if len(r) > 1 else ""
            code = str(r[2]) if len(r) > 2 else ""
        out.append({"name": name, "code": code})
        if len(out) >= limit:
            break
    return out


def rotation_group(D, code, max_n=15):
    """族群輪動全集：同產業 peers ＋ 上游供應商 ＋ 下游客戶（去重、排除自身）"""
    info = sector_info(D, code)
    if not info:
        return None
    members = {"peers": [], "upstream": [], "downstream": []}
    seen = {code}
    for c in sector_peers(D, code):
        if c not in seen:
            members["peers"].append({"code": c, "name": D[c].get("n", "")})
            seen.add(c)
    for u in chain_names(info["upstream"], kind="up"):
        cu = u["code"]
        if cu and cu not in seen and cu in D:
            members["upstream"].append({"code": cu, "name": u["name"], "note": u["name"]})
            seen.add(cu)
    for dnm in chain_names(info["downstream"], kind="dn"):
        cd = dnm["code"]
        if cd and cd not in seen and cd in D:
            members["downstream"].append({"code": cd, "name": dnm["name"]})
            seen.add(cd)
    members["peers"] = members["peers"][:max_n]
    members["upstream"] = members["upstream"][:max_n]
    members["downstream"] = members["downstream"][:max_n]
    members["info"] = {k: info[k] for k in ("code", "name", "ind", "pos")}
    return members
