"""Telegram 分享入口：把網址、文章全文或論文 PDF 傳到 bot 的私聊，就加入待處理。

只寫入 queue，不觸發轉錄（本機服務平常不開，回家手動處理）。三種內容：
- 網址：與手機網頁貼網址相同，網址以外的短文字存成備註
- 全文：網址以外的文字超過備註上限就視為文章全文（kind=text），也可傳 .txt／.md 檔。
  Telegram 會把超過 4096 字的訊息拆成數則連續送出，MERGE_WINDOW 內接著來的文字併入同一筆
- PDF：傳檔案（kind=pdf），原檔暫存在 queue.pdf，本機上傳該篇後隨項目刪除。
  episode_guid 與本機 storage.paper_guid 同算法，入列時就填好，重複判斷與結案都靠它

bot 沿用本機完成推播的同一個（src/podscript/notify.py），設定也讀同一組：
TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 優先讀環境變數，沒有再讀資料庫 app_settings 表。

TELEGRAM_WEBHOOK_SECRET 只放環境變數，未設定時此端點一律 404，等於關閉。
bot 誰都搜得到，因此只接受 TELEGRAM_CHAT_ID 那個聊天室的訊息，其他來源靜默忽略。
"""
import hashlib
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
API_URL = "https://api.telegram.org/bot{token}/{method}"
FILE_URL = "https://api.telegram.org/file/bot{token}/{path}"
TIMEOUT = 10
FILE_TIMEOUT = 60

# Bot API 只能下載 20 MB 以內的檔案
MAX_FILE_BYTES = 20 * 1024 * 1024
TEXT_EXTENSIONS = (".txt", ".md")
# 拆開的長訊息幾乎同時送達；窗口放短，避免把隔一陣子貼的另一篇併進來
MERGE_WINDOW_SECONDS = 15

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


def strip_urls(text: str, urls: list[str]) -> str:
    """去掉網址後的文字，空白收成一格。"""
    for url in urls:
        # 補上 scheme 的網址在原文中沒有 https://，兩種都要拿掉
        text = text.replace(url, " ").replace(re.sub(r"^https?://", "", url, flags=re.I), " ")
    text = URL_PATTERN.sub(" ", text)
    return " ".join(text.split())


def _add_url(url: str, note: str | None) -> str:
    """加入一筆網址並回傳要回覆的那一行。"""
    try:
        url = _validate_url(url)
        with get_connection() as conn:
            with conn.cursor() as cur:
                _, created = insert_item(cur, url, note, None, [])
                conn.commit()
    except HTTPException as exc:
        return f"{exc.detail}：{url}" if exc.status_code == 400 else exc.detail
    return f"已加入待處理：{url}" if created else f"已在待處理清單中：{url}"


def _append_text(cur, text: str) -> int | None:
    """併入 MERGE_WINDOW 內最近一筆全文，回傳併入後的字數；沒有可併的就回傳 None。"""
    cur.execute(
        "update queue set content = content || %s where id = ("
        "  select id from queue where kind = 'text' and status = 'pending'"
        "  and created_at > now() - make_interval(secs => %s)"
        "  order by created_at desc limit 1"
        ") returning length(content)",
        ("\n" + text, MERGE_WINDOW_SECONDS),
    )
    row = cur.fetchone()
    return row[0] if row else None


def _add_text(text: str, *, merge: bool = True) -> str:
    """加入一篇全文（或併入剛才拆開送來的上一段），回傳要回覆的那一行。"""
    with get_connection() as conn:
        with conn.cursor() as cur:
            length = _append_text(cur, text) if merge else None
            if length is None:
                # 標題取第一行，本機處理時會改成正式標題
                title = text.strip().splitlines()[0][:40]
                cur.execute(
                    "insert into queue (kind, content, title) values ('text', %s, %s)",
                    (text, title),
                )
            conn.commit()
    if length is not None:
        return f"已併入上一則全文（共 {length} 字）"
    return f"已加入待處理（全文 {len(text)} 字）：{title}"


def _add_pdf(data: bytes, filename: str, note: str | None) -> str:
    if not data.startswith(b"%PDF-"):
        return f"不是有效的 PDF：{filename}"
    guid = "paper-" + hashlib.sha256(data).hexdigest()[:16]
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("select title from episodes where episode_guid = %s", (guid,))
            existing = cur.fetchone()
            if existing:
                return f"這篇已經處理過了：{existing[0]}"
            cur.execute(
                "select 1 from queue where episode_guid = %s and status = 'pending'", (guid,)
            )
            if cur.fetchone():
                return f"已在待處理清單中：{filename}"
            cur.execute(
                "insert into queue (kind, pdf, title, note, episode_guid)"
                " values ('pdf', %s, %s, %s, %s)",
                (data, filename, note, guid),
            )
            conn.commit()
    return f"已加入待處理（PDF）：{filename}"


def _download(token: str, file_id: str) -> bytes:
    resp = httpx.get(
        API_URL.format(token=token, method="getFile"), params={"file_id": file_id}, timeout=TIMEOUT
    )
    resp.raise_for_status()
    path = resp.json()["result"]["file_path"]
    resp = httpx.get(FILE_URL.format(token=token, path=path), timeout=FILE_TIMEOUT)
    resp.raise_for_status()
    return resp.content


def _handle_document(token: str, document: dict, caption: str) -> str:
    filename = document.get("file_name") or "未命名"
    lower = filename.lower()
    is_pdf = document.get("mime_type") == "application/pdf" or lower.endswith(".pdf")
    is_text = lower.endswith(TEXT_EXTENSIONS)
    if not is_pdf and not is_text:
        return f"不支援這種檔案：{filename}（可傳 PDF、.txt、.md）"
    if (document.get("file_size") or 0) > MAX_FILE_BYTES:
        return f"檔案超過 20 MB（Telegram bot 的下載上限），請回家用本機網頁上傳：{filename}"

    try:
        data = _download(token, document["file_id"])
    except httpx.HTTPError as exc:
        # 例外訊息含完整網址（帶 token），只記類別
        logger.warning("Telegram 檔案下載失敗：%s", exc.__class__.__name__)
        return f"檔案下載失敗，請再傳一次：{filename}"

    note = caption.strip()[:MAX_NOTE_LENGTH] or None
    if is_pdf:
        return _add_pdf(data, filename, note)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return f"文字檔須為 UTF-8 編碼：{filename}"
    if not text.strip():
        return f"檔案是空的：{filename}"
    return _add_text(text, merge=False)


def _handle_text(text: str, entities: list[dict]) -> str:
    urls = extract_urls(text, entities)
    rest = strip_urls(text, urls)
    # 網址以外的文字放得進備註，就是「分享網址附帶標題」；放不下就是貼了文章全文
    if len(rest) > MAX_NOTE_LENGTH:
        return _add_text(text)
    if urls:
        note = rest or None
        return "\n".join(_add_url(url, note) for url in urls)
    if text.strip():
        # 拆開的長訊息最後一段可能很短
        with get_connection() as conn:
            with conn.cursor() as cur:
                length = _append_text(cur, text)
                conn.commit()
        if length is not None:
            return f"已併入上一則全文（共 {length} 字）"
    return f"沒有找到網址。可分享單集連結、貼上文章全文（超過 {MAX_NOTE_LENGTH} 字），或傳 PDF"


def _reply(token: str, chat_id: str, text: str) -> None:
    """回覆失敗只記 log：內容已經入列，不該讓 Telegram 因此重送。"""
    try:
        httpx.post(
            API_URL.format(token=token, method="sendMessage"),
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

    if message.get("document"):
        reply = _handle_document(token, message["document"], message.get("caption") or "")
    else:
        # 分享帶圖時網址在 caption
        text = message.get("text") or message.get("caption") or ""
        entities = message.get("entities") or message.get("caption_entities") or []
        reply = _handle_text(text, entities)
    _reply(token, chat_id, reply)
    return {"ok": True}
