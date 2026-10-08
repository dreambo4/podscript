"""論文 PDF 原檔的存放：Supabase Storage 的 papers bucket。

資料庫只存抽出的文字，PDF 原檔放 Storage，兩台電腦與手機都從這裡取得。
物件名稱為 `<guid>.pdf`，guid 由檔案內容雜湊產生，同一份 PDF 重複上傳只會覆蓋同一個物件。

Storage 不能用 DATABASE_URL 直連存取，改走 Storage API，
需要 `.env` 的 SUPABASE_URL 與 SUPABASE_SECRET_KEY（secret key 不受 RLS 限制，只放本機）。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

import httpx

BUCKET = "papers"
TIMEOUT = 60


class StorageError(Exception):
    """Storage 未設定，或上傳、下載失敗。"""


@dataclass
class StoredPaper:
    """Storage 中的一份論文原檔。

    Attributes:
        guid: 物件名稱去掉 `.pdf`，與 episodes.episode_guid 相同。
        size: 位元組數。
        metadata: 上傳時附帶的原始檔名與下載來源。
    """

    guid: str
    size: int
    metadata: dict


def paper_guid(pdf: bytes) -> str:
    """以 PDF 內容雜湊產生 guid，前綴 `paper-`。"""
    return "paper-" + hashlib.sha256(pdf).hexdigest()[:16]


def upload_paper(path: Path, *, source_url: str | None = None) -> str:
    """上傳 PDF 原檔，回傳 guid；已存在時覆蓋。

    Args:
        source_url: 論文的下載來源，存在物件的 metadata。

    Raises:
        StorageError: 不是 PDF、未設定金鑰或上傳失敗。
    """
    data = path.read_bytes()
    if not data.startswith(b"%PDF-"):
        raise StorageError(f"不是 PDF 檔：{path.name}")

    guid = paper_guid(data)
    metadata = {"filename": path.name}
    if source_url:
        metadata["source_url"] = source_url

    response = _request(
        "POST",
        f"object/{BUCKET}/{guid}.pdf",
        content=data,
        headers={
            "Content-Type": "application/pdf",
            "x-upsert": "true",
            "x-metadata": base64.b64encode(
                json.dumps(metadata, ensure_ascii=False).encode()
            ).decode(),
        },
    )
    _raise_for_status(response, "上傳")
    return guid


def download_paper(guid: str, dest: Path) -> Path:
    """下載 PDF 原檔到 dest，回傳寫入的路徑。

    Raises:
        StorageError: 未設定金鑰、找不到檔案或下載失敗。
    """
    response = _request("GET", f"object/authenticated/{BUCKET}/{guid}.pdf")
    _raise_for_status(response, "下載")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(response.content)
    return dest


def list_papers() -> list[StoredPaper]:
    """列出 bucket 中所有論文原檔，新上傳的在前。

    Raises:
        StorageError: 未設定金鑰或查詢失敗。
    """
    response = _request(
        "POST",
        f"object/list/{BUCKET}",
        json={
            "prefix": "",
            "limit": 1000,
            "sortBy": {"column": "created_at", "order": "desc"},
        },
    )
    _raise_for_status(response, "列出")
    # list 不回傳上傳時附帶的 metadata，逐筆以 info 取得；論文數量少，不影響速度。
    papers = []
    for item in response.json():
        if not item["name"].endswith(".pdf"):
            continue
        info = _request("GET", f"object/info/{BUCKET}/{item['name']}")
        _raise_for_status(info, "查詢")
        detail = info.json()
        papers.append(
            StoredPaper(
                guid=item["name"].removesuffix(".pdf"),
                size=detail.get("size", 0),
                metadata=detail.get("metadata") or {},
            )
        )
    return papers


def _request(method: str, path: str, **kwargs) -> httpx.Response:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SECRET_KEY", "")
    if not url or not key:
        raise StorageError("未設定 SUPABASE_URL 或 SUPABASE_SECRET_KEY，見 .env.example")

    headers = {"apikey": key, **kwargs.pop("headers", {})}
    # 新版 secret key（sb_secret_）不是 JWT，只能放 apikey；舊版 service_role 是 JWT，兩處都要帶。
    if not key.startswith("sb_"):
        headers["Authorization"] = f"Bearer {key}"

    try:
        return httpx.request(
            method,
            f"{url}/storage/v1/{path}",
            headers=headers,
            timeout=TIMEOUT,
            **kwargs,
        )
    except httpx.HTTPError as exc:
        raise StorageError(f"連不上 Supabase Storage：{exc}") from exc


def _raise_for_status(response: httpx.Response, action: str) -> None:
    if response.is_success:
        return
    try:
        message = response.json().get("message") or response.text
    except ValueError:
        message = response.text
    raise StorageError(f"{action}失敗（HTTP {response.status_code}）：{message}")
