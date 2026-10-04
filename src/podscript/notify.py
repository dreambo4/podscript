"""處理完成或失敗時推播到 Telegram。

長時間轉錄時人常不在電腦前，用 Telegram bot 傳訊息到手機。
TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 存在資料庫 app_settings 表，換電腦不必重填；
.env 有同名變數時以 .env 為準。兩處都沒有時直接略過。
發送失敗只記 log，不可影響轉錄結果。訊息只帶標題與狀態，不帶逐字稿內容。
"""
from __future__ import annotations

import logging
import os

import requests

from . import upload

logger = logging.getLogger(__name__)

API_URL = "https://api.telegram.org/bot{token}/sendMessage"
TIMEOUT = 10
# Telegram 單則上限 4096 字；錯誤訊息可能夾帶整段堆疊，截短即可
MAX_ERROR_CHARS = 500


SETTING_KEYS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")

_db_cache: dict[str, str] = {}


def _db_settings() -> dict[str, str]:
    """資料庫中的推播設定。查到後快取到服務重啟，改設定須重啟服務生效；
    查詢失敗（空結果）不快取，下次推播再試，避免一次連線失敗就整段期間不推播。"""
    if not _db_cache:
        _db_cache.update(upload.fetch_settings(list(SETTING_KEYS)))
    return _db_cache


def _setting(key: str) -> str | None:
    return os.environ.get(key) or _db_settings().get(key)


def send(text: str) -> bool:
    """發送一則純文字訊息。回傳是否成功送出；未設定時回傳 False。"""
    token = _setting("TELEGRAM_BOT_TOKEN")
    chat_id = _setting("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False

    try:
        resp = requests.post(
            API_URL.format(token=token),
            json={"chat_id": chat_id, "text": text},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        # 例外訊息含完整網址（帶 token），只記狀態碼與類別
        status = getattr(exc.response, "status_code", None)
        logger.warning("Telegram 推播失敗：%s %s", exc.__class__.__name__, status or "")
        return False
    return True


def job_done(title: str, minutes: int) -> bool:
    return send(f"轉錄完成：{title}（耗時 {minutes} 分）")


def job_failed(title: str, error: str) -> bool:
    if len(error) > MAX_ERROR_CHARS:
        error = error[:MAX_ERROR_CHARS] + "…"
    return send(f"處理失敗：{title}：{error}")
