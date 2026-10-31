# -*- coding: utf-8 -*-
"""Telegram 通知（.env：TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID）— 未設定時靜默略過"""
import os
import urllib.parse
import urllib.request

from logutil import get_logger, ensure_utf8_stdout

ensure_utf8_stdout()
log = get_logger("notify")


def configured():
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def send(msg):
    """送出 Telegram 訊息；未設定或失敗時不丟例外（只記 log），不影響交易流程"""
    if not configured():
        log.debug("Telegram 未設定 → 略過通知")
        return False
    try:
        url = f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": os.environ["TELEGRAM_CHAT_ID"],
                                       "text": msg[:4000]}).encode()
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10) as r:
            r.read()
        log.debug(f"Telegram 已送出：{msg[:60]}")
        return True
    except Exception as e:
        log.warning(f"Telegram 通知失敗：{e}")
        return False
