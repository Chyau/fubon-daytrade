# -*- coding: utf-8 -*-
"""export_chips.py — 產生籌碼快照（chips_export.json），供雲端儀表板 🧲 籌碼選股分頁

依賴本機的 chips 快取（daily_update.bat 後最新）；失敗時不產出檔案（雲端分頁顯示提示）。
用法：python tools/export_chips.py
"""
import io
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

OUT = BASE / "data" / "chips_export.json"


def main():
    import chips as chips_mod
    D = chips_mod.load_dataset()
    payload = {"generated": datetime.now().strftime("%Y-%m-%d %H:%M")}

    # ① 隔日沖候選（漲停 ∩ 分點買超）
    try:
        rows = chips_mod.daytrade_short_candidates(D, 50, 15)
        payload["candidates"] = [{
            "code": info["code"], "name": info["name"],
            "close": info.get("close"), "chg": info.get("chg_pct"),
            "vol": info.get("volume"), "buy": buy} for buy, info in rows]
    except Exception as e:
        payload["candidates"] = []
        print(f"候選取得失敗：{str(e)[:60]}")

    # ② 觀察清單（build_watchlist：籌碼＋掃描器合流）
    try:
        _, lines = chips_mod.build_watchlist(D, min_branch=30, top_n=10,
                                             min_avg_vol=2000)
        wl = []
        for ln in lines:
            parts = ln.split()
            if not parts:
                continue
            code = parts[0]
            import re as _re
            m_price = _re.search(r"\$(\d+(?:\.\d+)?)", ln)
            m_chg = _re.search(r"([+\-]?\d+(?:\.\d+)?)%", ln)
            m_vol = _re.search(r"avgVol=(\d+)", ln)
            name = parts[1] if len(parts) > 1 else ""
            note = ln.split("｜", 1)[1] if "｜" in ln else ""
            wl.append({"code": code, "name": name,
                       "price": m_price.group(1) if m_price else "—",
                       "chg": float(m_chg.group(1)) if m_chg else 0,
                       "vol": int(m_vol.group(1)) if m_vol else 0,
                       "note": note[:40]})
        payload["watchlist"] = wl
    except Exception as e:
        payload["watchlist"] = []
        print(f"觀察清單失敗：{str(e)[:60]}")

    # ③ 漲停隔日統計（文字表）
    try:
        stats, _rows = chips_mod.fetch_nextday()
        payload["nextday"] = str(stats)[:600]
    except Exception as e:
        print(f"漲停隔日統計失敗：{str(e)[:60]}")

    OUT.write_text(__import__("json").dumps(payload, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"✓ {OUT}")
    print(f"  候選 {len(payload.get('candidates', []))}｜觀察 {len(payload.get('watchlist', []))}"
          f"｜漲停統計 {'✓' if payload.get('nextday') else '—'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
