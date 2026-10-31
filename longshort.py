# -*- coding: utf-8 -*-
"""多空分類橋接：讀取使用者 tw_stock.db 的 longshort_signal 表
（來源工具：D:\\權證小哥-當沖飆股神手\\query_longshort.py，
 bias_label ∈ 做多/偏多/觀望/偏空/做空，做空分數最低、偏多次之）

用法：
    import longshort
    date, rows = longshort.load_longshort()   # 詳細清單（dict list）
    date, syms = longshort.short_symbols()    # "1301,1303,..."（做空+偏空，做空優先）
"""
import json
import os
import sqlite3

DEFAULT_DB = r"D:\權證小哥-當沖飆股神手\twdata\tw_stock.db"
SHORT_LABELS = ("做空", "偏空")


def _db_path():
    return os.environ.get("LONGSHORT_DB", DEFAULT_DB)


def load_longshort(date=None, labels=SHORT_LABELS, db_path=None):
    """回傳 (基準日 'YYYY/MM/DD' 或 None, [ {stock_no, bias_label, bias_score, tags, close} ])

    排序：做空排偏空前；同組內 bias_score 越低（越空）越前。
    DB 不存在或查無資料 → (None, [])（呼叫端自行後備，不拋錯）。
    """
    path = db_path or _db_path()
    if not os.path.exists(path):
        return None, []
    try:
        con = sqlite3.connect(path)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        if not date:
            row = cur.execute("SELECT MAX(date) FROM longshort_signal").fetchone()
            date = row[0] if row and row[0] else None
        if not date:
            con.close()
            return None, []
        q = ("SELECT stock_no, bias_label, bias_score, tags_json, close "
             "FROM longshort_signal WHERE date=? AND bias_label IN (%s)"
             % ",".join("?" * len(labels)))
        rows = [dict(r) for r in cur.execute(q, (date, *labels)).fetchall()]
        con.close()
        for r in rows:
            try:
                r["tags"] = [t[0] for t in json.loads(r.pop("tags_json") or "[]")]
            except Exception:
                r["tags"] = []
        rows.sort(key=lambda r: (r["bias_label"] != "做空",
                                 r["bias_score"] if r["bias_score"] is not None else 0))
        return date, rows
    except Exception:
        return None, []


def short_symbols(date=None, db_path=None):
    """回傳 (date, "1301,1303,...") — 做空+偏空合併清單（做空優先、越空越前）"""
    d, rows = load_longshort(date, db_path=db_path)
    return d, ",".join(r["stock_no"] for r in rows)


if __name__ == "__main__":
    try:
        sys_out = __import__("sys").stdout
        import io
        sys_out = io.TextIOWrapper(sys_out.buffer, encoding="utf-8", errors="replace")
        __import__("sys").stdout = sys_out
    except Exception:
        pass
    d, rows = load_longshort()
    print(f"基準日：{d}｜做空+偏空 {len(rows)} 檔")
    for r in rows:
        print(f"  {r['stock_no']} {r['bias_label']} score={r['bias_score']}"
              f" close={r['close']} tags={'/'.join(r['tags'][:4])}")
    d2, syms = short_symbols()
    print(f"\nsymbols 字串：{syms}")
