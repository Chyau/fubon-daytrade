# -*- coding: utf-8 -*-
"""股票代碼後綴查詢：上市→.TW、上櫃/興櫃→.TWO

資料來源：D:\\stock_db\\csv_data\\code\\*.csv（Big5 編碼，
欄位依位置：0=code, 1=name, 2=market；market ∈ 上市/上市臺灣創新板/上櫃/興櫃）
（源自使用者提供之 modify_ticker() 參考實作，D:\\stock_db）

yfinance 盲試 .TW/.TWO 有兩個坑：
  1. 上櫃股（如 4966）盲試 .TW 會 Timeout/404 才輪到 .TWO
  2. 上市股（如 3008）盲試 .TWO 直接 404，還會觸發 Yahoo 限流
本模組用本地對照表一次查對，雲端（無 D:\\）自動退回盲試順序。

用法：
    import ticker_db
    ticker_db.modify_ticker("4966")     # → "4966.TWO"
    ticker_db.yf_candidates("3008")     # → ["3008.TW"]（不浪費 .TWO 請求）
    ticker_db.show_name("2330")         # → "台積電"
"""
import glob
import os

STOCK_DB_PATH = os.environ.get("STOCK_DB_PATH", r"D:\stock_db")
_cache = None  # {code: (name, market)}， None=尚未載入


def _read_lines(fp):
    """CSV 可能是 UTF-8（實測 2026-04-11 檔）或 Big5；utf-8-sig 優先，解不動退 big5"""
    for enc in ("utf-8-sig", "big5"):
        try:
            with open(fp, "r", encoding=enc) as f:
                return f.read().splitlines()
        except UnicodeDecodeError:
            continue
    return []


def _load_cache():
    """掃 csv_data/code/*.csv 建對照表（每程序一次）。無檔案→空 dict。"""
    global _cache
    if _cache is not None:
        return _cache
    _cache = {}
    try:
        files = glob.glob(os.path.join(STOCK_DB_PATH, "csv_data", "code", "*.csv"))
        for fp in files:
            try:
                lines = _read_lines(fp)
                for i, line in enumerate(lines):
                    if i == 0 and not line.split(",")[0].strip().isdigit():
                        continue  # 表頭（BOM+code,name,market,...）
                    p = line.rstrip("\r\n").split(",")
                    if len(p) < 3:
                        continue
                    code, name, market = p[0].strip(), p[1].strip(), p[2].strip()
                    if code and code[0].isdigit():
                        _cache[code] = (name, market)
            except Exception:
                continue
    except Exception:
        pass
    return _cache


def _lookup(sym):
    code = str(sym).split(".")[0]
    return _load_cache().get(code)


def modify_ticker(sym):
    """上市/上市臺灣創新板 → {code}.TW；上櫃/興櫃 → {code}.TWO；查無 → None"""
    info = _lookup(sym)
    if not info:
        return None
    name, market = info          # 對照表 tuple = (name, market)
    code = str(sym).split(".")[0]
    if market in ("上市", "上市臺灣創新板"):
        return f"{code}.TW"
    if market in ("上櫃", "興櫃"):
        return f"{code}.TWO"
    return None


def show_name(sym):
    """從本地對照表查股票名稱（查無→空字串）"""
    info = _lookup(sym)
    return info[0] if info else ""


def yf_candidates(sym):
    """yf 下載候選順序：
    已帶後綴 → 原樣；本機對照表可查 → 正確後綴優先（不發多餘請求）；
    查不到（雲端/新上市）→ 盲試 .TW/.TWO 後備。"""
    sym = str(sym)
    if "." in sym:
        return [sym]
    out = []
    t = modify_ticker(sym)
    if t:
        out.append(t)
    out.extend([f"{sym}.TW", f"{sym}.TWO"])
    # 去重保持順序（對照表結果通常與盲試第一位相同）
    return list(dict.fromkeys(out))


if __name__ == "__main__":
    try:
        import io
        import sys
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass
    d = _load_cache()
    print(f"對照表載入：{len(d)} 檔（來源 {STOCK_DB_PATH}）")
    for s in ("2330", "4966", "3008", "1101", "9999"):
        t = modify_ticker(s)
        print(f"  {s} → {t or '（查無→盲試後備）'}  名稱：{show_name(s)}")
    print("yf_candidates(3008) =", yf_candidates("3008"))
    print("yf_candidates(4966) =", yf_candidates("4966"))
