"""平台解析層。新增平台時實作 PlatformResolver 並加入 _RESOLVERS。"""
from __future__ import annotations

from .apple import ApplePodcastResolver
from .base import Episode, PlatformResolver, ResolveError

_RESOLVERS: list[PlatformResolver] = [
    ApplePodcastResolver(),
]


def resolve(url: str) -> Episode:
    """依網址選用對應平台的 resolver。

    Raises:
        ResolveError: 無支援的平台，或該平台解析失敗。
    """
    url = url.strip()
    for resolver in _RESOLVERS:
        if resolver.can_handle(url):
            return resolver.resolve(url)
    raise ResolveError(f"不支援的網址：{url}（目前僅支援 Apple Podcast）")


__all__ = ["Episode", "PlatformResolver", "ResolveError", "resolve"]
