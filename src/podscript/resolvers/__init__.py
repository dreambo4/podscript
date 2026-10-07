"""平台解析層。新增平台時實作 PlatformResolver 並加入 _RESOLVERS。"""
from __future__ import annotations

from pathlib import Path

from ..audio import AudioError
from .apple import ApplePodcastResolver
from .base import Episode, PlatformResolver, ResolveError
from .youtube import YouTubeResolver

_RESOLVERS: list[PlatformResolver] = [
    ApplePodcastResolver(),
    YouTubeResolver(),
]


def is_media_url(url: str) -> bool:
    """是否為支援的影音平台網址；自動判斷類型時，其餘網址一律視為文章。"""
    url = url.strip()
    return any(resolver.can_handle(url) for resolver in _RESOLVERS)


def platform_of(url: str) -> str | None:
    """只看網址判斷平台，不連網；不是支援的影音平台時回傳 None。"""
    url = url.strip()
    for resolver in _RESOLVERS:
        if resolver.can_handle(url):
            return resolver.platform
    return None


def resolve(url: str) -> Episode:
    """依網址選用對應平台的 resolver。

    Raises:
        ResolveError: 無支援的平台，或該平台解析失敗。
    """
    url = url.strip()
    for resolver in _RESOLVERS:
        if resolver.can_handle(url):
            return resolver.resolve(url)
    raise ResolveError(f"不支援的網址：{url}（目前支援 Apple Podcast 與 YouTube）")


def download(episode: Episode, target: Path) -> None:
    """依單集的來源平台下載音檔到 target。

    Raises:
        AudioError: 下載失敗，或找不到該平台的 resolver。
    """
    for resolver in _RESOLVERS:
        if resolver.platform == episode.platform:
            resolver.download(episode, target)
            return
    raise AudioError(f"未知的平台 {episode.platform}，無法下載音檔")


__all__ = ["Episode", "PlatformResolver", "ResolveError", "download", "resolve"]
