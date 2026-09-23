"""Apple Podcast 網址解析。

兩段式查詢：iTunes Lookup API 為主，查無結果時改以 RSS 比對。
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

from .base import Episode, PlatformResolver, ResolveError

LOOKUP_API = "https://itunes.apple.com/lookup"
# Lookup API 的 podcastEpisode 查詢最多回傳 200 集，較舊的單集需改走 RSS。
LOOKUP_EPISODE_LIMIT = 200
# Lookup API 未帶 country 時只查美國區；僅在特定地區上架的節目會查無結果。
DEFAULT_COUNTRY = "tw"
TIMEOUT = 30

_ITUNES_NS = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"


class ApplePodcastResolver(PlatformResolver):
    platform = "apple"

    def can_handle(self, url: str) -> bool:
        return "podcasts.apple.com" in url

    def resolve(self, url: str) -> Episode:
        podcast_id, episode_id = self._parse_url(url)
        country = _parse_country(url)
        podcast_name, feed_url = self._lookup_podcast(podcast_id, country)

        episode = self._from_lookup(podcast_id, episode_id, podcast_name, url, country)
        if episode is None:
            episode = self._from_rss(feed_url, episode_id, podcast_name, url)
        return episode

    def _parse_url(self, url: str) -> tuple[str, str]:
        """從網址取出 podcast id 與單集 id。"""
        podcast_match = re.search(r"/id(\d+)", url)
        episode_match = re.search(r"[?&]i=(\d+)", url)
        if not podcast_match:
            raise ResolveError("網址中找不到 podcast id（預期格式 .../id1605731163）")
        if not episode_match:
            raise ResolveError("網址中找不到單集 id（預期格式 ...?i=1000789515808）；請複製單集頁面而非節目首頁的網址")
        return podcast_match.group(1), episode_match.group(1)

    def _lookup_podcast(self, podcast_id: str, country: str) -> tuple[str, str]:
        """查節目層資訊，取得節目名稱與 RSS 位址。"""
        resp = requests.get(
            LOOKUP_API,
            params={"id": podcast_id, "entity": "podcast", "country": country},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if not results:
            raise ResolveError(f"iTunes 查無此節目（id={podcast_id}）")

        info = results[0]
        feed_url = info.get("feedUrl")
        if not feed_url:
            raise ResolveError(f"節目《{info.get('collectionName')}》未公開 RSS，無法取得音檔")
        return info.get("collectionName", ""), feed_url

    def _from_lookup(
        self,
        podcast_id: str,
        episode_id: str,
        podcast_name: str,
        source_url: str,
        country: str,
    ) -> Episode | None:
        """以 Lookup API 的單集列表比對 trackId。查無對應單集時回傳 None。"""
        resp = requests.get(
            LOOKUP_API,
            params={
                "id": podcast_id,
                "entity": "podcastEpisode",
                "limit": LOOKUP_EPISODE_LIMIT,
                "country": country,
            },
            timeout=TIMEOUT,
        )
        resp.raise_for_status()

        for item in resp.json().get("results", []):
            if item.get("wrapperType") != "podcastEpisode":
                continue
            if str(item.get("trackId")) != episode_id:
                continue

            mp3_url = item.get("episodeUrl")
            if not mp3_url:
                return None

            millis = item.get("trackTimeMillis")
            return Episode(
                platform=self.platform,
                source_url=source_url,
                episode_guid=item.get("episodeGuid") or episode_id,
                podcast_name=podcast_name,
                title=item.get("trackName", ""),
                mp3_url=mp3_url,
                duration_sec=round(millis / 1000) if millis else None,
                published_at=_parse_iso8601(item.get("releaseDate")),
                description=item.get("description", ""),
            )
        return None

    def _from_rss(
        self, feed_url: str, episode_id: str, podcast_name: str, source_url: str
    ) -> Episode:
        """RSS 比對。用於 Lookup 涵蓋範圍外的舊單集。

        Apple 的單集 id 不會出現在 RSS 中，因此改以 Apple 頁面標題與 RSS 標題比對。
        """
        title = self._fetch_apple_title(episode_id)

        resp = requests.get(feed_url, timeout=TIMEOUT)
        resp.raise_for_status()
        channel = ET.fromstring(resp.content).find("channel")
        if channel is None:
            raise ResolveError("RSS 格式異常：找不到 channel 節點")

        for item in channel.findall("item"):
            item_title = _text(item, "title")
            if not item_title or not _title_matches(item_title, title):
                continue

            enclosure = item.find("enclosure")
            mp3_url = enclosure.get("url") if enclosure is not None else None
            if not mp3_url:
                raise ResolveError(f"RSS 中《{item_title}》沒有音檔連結")

            guid = _text(item, "guid") or episode_id
            return Episode(
                platform=self.platform,
                source_url=source_url,
                episode_guid=guid,
                podcast_name=podcast_name,
                title=item_title,
                duration_sec=_parse_duration(_text(item, f"{_ITUNES_NS}duration")),
                mp3_url=mp3_url,
                published_at=_parse_rfc2822(_text(item, "pubDate")),
                description=_text(item, "description") or "",
            )

        raise ResolveError(f"RSS 中找不到單集「{title}」；節目可能已下架該集")

    def _fetch_apple_title(self, episode_id: str) -> str:
        """從 Apple 單集頁面的 <title> 取出單集標題。"""
        resp = requests.get(
            f"https://podcasts.apple.com/tw/podcast/id0?i={episode_id}",
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()

        match = re.search(r"<title>(.*?)</title>", resp.text, re.S)
        if not match:
            raise ResolveError("無法從 Apple 頁面取得單集標題")

        # 形如：Apple Podcast：《節目名》〈單集標題⋯〉
        inner = re.search(r"〈(.*?)〉", match.group(1))
        if not inner:
            raise ResolveError("Apple 頁面標題格式非預期，無法取出單集名稱")
        return inner.group(1).strip()


def _parse_country(url: str) -> str:
    """從網址取出商店地區碼（podcasts.apple.com/tw/podcast/...），缺少時用預設值。"""
    match = re.search(r"podcasts\.apple\.com/([a-z]{2})/", url, re.I)
    return match.group(1).lower() if match else DEFAULT_COUNTRY


def _text(element: ET.Element, tag: str) -> str | None:
    node = element.find(tag)
    return node.text.strip() if node is not None and node.text else None


def _title_matches(rss_title: str, apple_title: str) -> bool:
    """比對標題。Apple 頁面標題過長時會以 ⋯ 截斷，故採前綴比對。"""
    truncated = apple_title.rstrip("⋯…").strip()
    return rss_title.startswith(truncated) or rss_title == apple_title


def _parse_iso8601(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_rfc2822(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _parse_duration(value: str | None) -> int | None:
    """itunes:duration 可能是秒數或 HH:MM:SS / MM:SS。"""
    if not value:
        return None
    if ":" not in value:
        return int(value) if value.isdigit() else None

    parts = value.split(":")
    if not all(p.isdigit() for p in parts):
        return None

    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return seconds
