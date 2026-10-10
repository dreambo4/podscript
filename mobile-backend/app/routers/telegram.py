"""Telegram 分享入口：把單集網址分享到 bot 的私聊，就加入待處理。

與手機網頁貼網址的結果相同，只寫入 queue，不觸發轉錄（本機服務平常不開，回家手動處理）。
bot 沿用本機完成推播的同一個（src/podscript/notify.py），設定也讀同一組：
TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 優先讀環境變數，沒有再讀資料庫 app_settings 表。

TELEGRAM_WEBHOOK_SECRET 只放環境變數，未設定時此端點一律 404，等於關閉。
bot 誰都搜得到，因此只接受 TELEGRAM_CHAT_ID 那個聊天室的訊息，其他來源靜默忽略。
"""
import hmac
import logging
import os
import re

import httpx
from fastapi import APIRouter, Body, Header, HTTPException

from ..database import get_connection
from .queue import MAX_NOTE_LENGTH, _validate_url, insert_item

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Telegram"])

SETTING_KEYS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"
TIMEOUT = 10

# 訊息沒有 entities 時（理論上 Telegram 都會標）的備援
URL_PATTERN = re.compile(r"https?://\S+")


def _settings() -> dict[str, str]:
    values = {k: os.environ[k] for k in SETTING_KEYS if os.environ.get(k)}
    missing = [k for k in SETTING_KEYS if k not in values]
    if missing:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("select key, value from app_settings where key = any(%s)", (missing,))
                values.update(dict(cur.fetchall()))
    return values


def extract_urls(text: str, entities: list[dict]) -> list[str]:
    """取出訊息中的網址，依出現順序、去重。

    Apple Podcast 分享出來的常是「標題＋網址」混在一起，不能整段當網址。
    entities 的 offset／length 以 UTF-16 code unit 計，中文與 emoji 會讓它和 Python 字串索引對不上，
    須換成 UTF-16 再切。
    """
    urls: list[str] = []
    encoded = text.encode("utf-16-le")
    for entity in entities:
        if entity.get("type") == "url":
            start, length = entity["offset"] * 2, entity["length"] * 2
            urls.append(encoded[start:start + length].decode("utf-16-le"))
        elif entity.get("type") == "text_link" and entity.get("url"):
            urls.append(entity["url"])
    if not urls:
        urls = URL_PATTERN.findall(text)
    # Telegram 會把 podcasts.apple.com/... 這類沒寫 scheme 的也標成 url
    urls = [u if re.match(r"https?://", u, re.I) else f"https://{u}" for u in urls]
    return list(dict.fromkeys(urls))


def extract_note(text: str, urls: list[str]) -> str | None:
    """網址以外的文字當備註（分享時附的標題或想聽的原因），超過上限就截短而非拒收。"""
    for url in urls:
        # 補上 scheme 的網址在原文中沒有 https://，兩種都要拿掉
        text = text.replace(url, " ").replace(re.sub(r"^https?://", "", url, flags=re.I), " ")
    text = URL_PATTERN.sub(" ", text)
    text = " ".join(text.split())
    if not text:
        return None
    if len(text) > MAX_NOTE_LENGTH:
        text = text[:MAX_NOTE_LENGTH - 1] + "…"
    return text


def _add(url: str, note: str | None) -> str:
    """加入一筆並回傳要回覆的那一行。"""
    try:
        url = _validate_url(url)
        with get_connection() as conn:
            with conn.cursor() as cur:
                _, created = insert_item(cur, url, note, None, [])
                conn.commit()
    except HTTPException as exc:
        return f"{exc.detail}：{url}" if exc.status_code == 400 else exc.detail
    return f"已加入待處理：{url}" if created else f"已在待處理清單中：{url}"


def _reply(token: str, chat_id: str, text: str) -> None:
    """回覆失敗只記 log：網址已經入列，不該讓 Telegram 因此重送。"""
    try:
        httpx.post(
            SEND_URL.format(token=token),
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=TIMEOUT,
        ).raise_for_status()
    except httpx.HTTPError as exc:
        # 例外訊息含完整網址（帶 token），只記類別與狀態碼
        status = getattr(getattr(exc, "response", None), "status_code", None)
        logger.warning("Telegram 回覆失敗：%s %s", exc.__class__.__name__, status or "")


@router.post("/telegram/webhook", include_in_schema=False)
def webhook(
    update: dict = Body(...),
    secret: str | None = Header(None, alias="X-Telegram-Bot-Api-Secret-Token"),
) -> dict:
    expected = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
    if not expected:
        raise HTTPException(status_code=404)
    if not secret or not hmac.compare_digest(secret, expected):
        raise HTTPException(status_code=403)

    message = update.get("message")
    if not message:
        return {"ok": True}

    settings = _settings()
    token = settings.get("TELEGRAM_BOT_TOKEN")
    chat_id = (settings.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat_id or str(message.get("chat", {}).get("id")) != chat_id:
        return {"ok": True}

    # 分享帶圖時網址在 caption
    text = message.get("text") or message.get("caption") or ""
    entities = message.get("entities") or message.get("caption_entities") or []
    urls = extract_urls(text, entities)
    if not urls:
        _reply(token, chat_id, "沒有找到網址，請分享單集連結")
        return {"ok": True}

    note = extract_note(text, urls)
    _reply(token, chat_id, "\n".join(_add(url, note) for url in urls))
    return {"ok": True}
