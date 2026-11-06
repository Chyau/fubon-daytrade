# -*- coding: utf-8 -*-
"""LLM 客戶端（OpenAI 相容格式：agnes-ai / Gemini OpenAI 端點 / 任何 /v1 相容服務）

.env 設定：
  LLM_BASE_URL   例 https://apihub.agnes-ai.com/v1（無尾斜線）
  LLM_API_KEY    金鑰（空 = AI 功能停用，所有呼叫靜默跳過，不影響交易流程）
  LLM_MODEL      例 agnes-2.5-flash
  LLM_TEMPERATURE（選，預設 0.3）／LLM_MAX_TOKENS（選，預設 1200）／LLM_TIMEOUT（選，秒）

設計原則：與 notify.py 同款 — 失敗不丟例外、只記 log，**絕不阻塞交易流程**。
"""
import json
import os
import urllib.request

from logutil import get_logger, ensure_utf8_stdout

ensure_utf8_stdout()
log = get_logger("llm")


def config():
    return {
        "base_url": (os.environ.get("LLM_BASE_URL") or "").rstrip("/"),
        "api_key": os.environ.get("LLM_API_KEY") or "",
        "model": os.environ.get("LLM_MODEL") or "",
        "temperature": float(os.environ.get("LLM_TEMPERATURE") or 0.3),
        "max_tokens": int(os.environ.get("LLM_MAX_TOKENS") or 1200),
        "timeout": int(os.environ.get("LLM_TIMEOUT") or 60),
    }


def enabled():
    """AI 功能開關：三個設定都在才啟用"""
    c = config()
    return bool(c["base_url"] and c["api_key"] and c["model"])


def chat(messages, temperature=None, max_tokens=None, json_mode=False):
    """呼叫 chat/completions。回傳 (text, None) 或 (None, error)。
    未啟用 → (None, 'LLM 未設定')；任何錯誤 → (None, err)（不丟例外）。"""
    c = config()
    if not enabled():
        return None, "LLM 未設定（LLM_BASE_URL / LLM_API_KEY / LLM_MODEL）"
    payload = {
        "model": c["model"],
        "messages": messages,
        "temperature": c["temperature"] if temperature is None else temperature,
        "max_tokens": c["max_tokens"] if max_tokens is None else max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    try:
        req = urllib.request.Request(
            f"{c['base_url']}/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {c['api_key']}"},
            method="POST")
        with urllib.request.urlopen(req, timeout=c["timeout"]) as r:
            body = json.loads(r.read().decode())
        text = (body.get("choices") or [{}])[0].get("message", {}).get("content")
        return (text or "").strip(), None
    except Exception as e:
        log.warning(f"LLM 呼叫失敗：{str(e)[:200]}")
        return None, str(e)[:200]


def quick(prompt, system=None, **kw):
    """單輪便捷呼叫：回傳 text 或 None（失敗/未啟用）"""
    msgs = ([{"role": "system", "content": system}] if system else [])
    msgs.append({"role": "user", "content": prompt})
    text, err = chat(msgs, **kw)
    if err:
        log.debug(f"quick 失敗：{err}")
    return text
