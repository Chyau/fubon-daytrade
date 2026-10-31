# -*- coding: utf-8 -*-
"""
共用 logging 設定
=================
- 檔案：logs/fubon_daytrade.log（RotatingFileHandler，DEBUG 全量，UTF-8，1MB×5）
- 主控台：預設 INFO，可用環境變數 LOG_LEVEL 或 --log-level 調整
- 模組以 get_logger("orders") / get_logger("backtest") 取子記錄器
"""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

_FMT = "%(asctime)s [%(levelname)-7s] %(name)s:%(lineno)d - %(message)s"


def ensure_utf8_stdout():
    """Windows 主控台 UTF-8 輸出（可重入：已包裝則跳過，避免重複包裝關閉底層 buffer）"""
    import io
    import sys
    enc = getattr(sys.stdout, "encoding", "") or ""
    if enc.lower().replace("-", "") == "utf8":
        return
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


def _setup(base: logging.Logger):
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(log_dir, exist_ok=True)
    base.setLevel(logging.DEBUG)  # 總閘全開，由 handler 決定輸出層級
    # 每程序獨立日誌檔（避免多實例輪替時 Windows 檔案鎖衝突）＋清理 7 天以上舊檔
    import glob
    import time as _t
    pid = os.getpid()
    for old in glob.glob(os.path.join(log_dir, "fubon_daytrade_*.log*")):
        try:
            if _t.time() - os.path.getmtime(old) > 7 * 86400:
                os.remove(old)
        except OSError:
            pass
    fh = RotatingFileHandler(os.path.join(log_dir, f"fubon_daytrade_{pid}.log"),
                             maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(_FMT))
    base.addHandler(fh)
    ch = logging.StreamHandler(sys.stdout)  # noqa: F821 — 於 _setup 內取用
    ch.setLevel(getattr(logging, (os.environ.get("LOG_LEVEL") or "INFO").upper(), logging.INFO))
    ch.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    base.addHandler(ch)
    base.propagate = False


def get_logger(child: str = None, level: str = None) -> logging.Logger:
    """取得（並在首次呼叫時初始化）共用記錄器。level 可即時調整主控台層級。"""
    base = logging.getLogger("fubon")
    if not base.handlers:
        _setup(base)
    if level:
        lv = getattr(logging, str(level).upper(), None)
        if lv:
            for h in base.handlers:
                if isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler):
                    h.setLevel(lv)
    return base.getChild(child) if child else base
