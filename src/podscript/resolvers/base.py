"""Podcast 平台解析層的介面定義。

新增平台只需實作 PlatformResolver 並註冊到 registry，下游流程不變。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Any


@dataclass
class Episode:
    """單集節目的解析結果。

    Attributes:
        platform: 來源平台代號，對應 Supabase episodes.platform。
        source_url: 使用者輸入的原始網址。
        episode_guid: 單集唯一識別碼，用於防重複轉錄與快取目錄命名。
        podcast_name: 節目名稱。
        title: 單集標題。
        mp3_url: 音檔直連網址。可能帶時效性參數，不保證長期有效。
        duration_sec: 音檔長度（秒）。RSS 未提供時為 None。
        published_at: 發布時間。RSS 未提供時為 None。
        description: 節目簡介原文，含 HTML 標記與贊助商段落。
    """

    platform: str
    source_url: str
    episode_guid: str
    podcast_name: str
    title: str
    mp3_url: str
    duration_sec: int | None = None
    published_at: datetime | None = None
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if self.published_at is not None:
            d["published_at"] = self.published_at.isoformat()
        return d


class ResolveError(Exception):
    """解析失敗。訊息需可直接顯示給使用者。"""


class PlatformResolver(ABC):
    """將平台網址解析為 Episode。"""

    platform: str

    @abstractmethod
    def can_handle(self, url: str) -> bool:
        """判斷是否為本平台的網址。"""

    @abstractmethod
    def resolve(self, url: str) -> Episode:
        """解析網址，取得 mp3 直連與節目資訊。

        Raises:
            ResolveError: 網址格式錯誤、查無節目，或 RSS 中找不到對應單集。
        """
