"""文章與新聞的解析：從網頁擷取正文，或接受使用者貼上的全文。

文章沒有音檔，不走 PlatformResolver 的下載流程；
解析結果直接帶著分好段的正文，交給 pipeline.process_article 產生摘要。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import trafilatura

from .base import Episode, ResolveError

PLATFORM = "article"

# 正文少於此字數視為擷取失敗：多半是付費牆、需要 JS 載入，或抓到的只是導覽頁。
MIN_CHARS = 300

# 分享連結常帶的追蹤參數，去掉後才能判斷是否同一篇。
TRACKING_PARAMS = re.compile(
    r"^(utm_\w+|fbclid|gclid|igshid|mc_cid|mc_eid|ref|ref_src|from|share)$", re.I
)


@dataclass
class Article:
    """文章解析結果。

    Attributes:
        episode: 對應到單集欄位的中繼資料，podcast_name 為媒體名稱。
        paragraphs: 依原文段落切開的正文。
    """

    episode: Episode
    paragraphs: list[str]


def resolve_url(url: str) -> Article:
    """抓取網頁並擷取正文、標題、媒體名稱與發布時間。

    Raises:
        ResolveError: 網頁抓不到，或正文太短（付費牆、需要 JS 載入等）。
    """
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        raise ResolveError(f"不是有效的網址：{url}")

    html = trafilatura.fetch_url(url)
    if not html:
        raise ResolveError("抓不到這個網頁，可改用「貼上全文」")

    document = trafilatura.bare_extraction(
        html, url=url, with_metadata=True, include_comments=False
    )
    text = (document.text if document else "") or ""
    paragraphs = _split_paragraphs(text)
    if sum(len(p) for p in paragraphs) < MIN_CHARS:
        raise ResolveError(
            "擷取到的正文太短，可能是付費文章或需要登入，可改用「貼上全文」"
        )

    host = urlsplit(url).hostname or ""
    title, sitename = _title_and_site(document.title or "", document.sitename or "", host)
    title = title or paragraphs[0][:40]
    # 正文第一段常是重複的標題
    if paragraphs[0] == title:
        paragraphs = paragraphs[1:]

    canonical = normalize_url(url)
    episode = Episode(
        platform=PLATFORM,
        source_url=canonical,
        episode_guid=_guid(canonical),
        podcast_name=sitename,
        title=title,
        mp3_url="",
        published_at=_parse_date(document.date),
    )
    return Article(episode=episode, paragraphs=paragraphs)


def from_text(text: str, *, title: str = "", source: str = "") -> Article:
    """以使用者貼上的全文建立文章。

    Args:
        title: 標題，留空則取正文第一段開頭。
        source: 媒體名稱，留空顯示「貼上的文章」。

    Raises:
        ResolveError: 沒有內容。
    """
    paragraphs = _split_paragraphs(text)
    if not paragraphs:
        raise ResolveError("沒有貼上任何內容")

    title = title.strip() or paragraphs[0][:40]
    episode = Episode(
        platform=PLATFORM,
        source_url="",
        episode_guid=_guid("\n".join(paragraphs)),
        podcast_name=source.strip() or "貼上的文章",
        title=title,
        mp3_url="",
    )
    return Article(episode=episode, paragraphs=paragraphs)


def normalize_url(url: str) -> str:
    """去掉追蹤參數與錨點，同一篇文章不同分享連結會得到同一個網址。"""
    parts = urlsplit(url.strip())
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not TRACKING_PARAMS.match(k)
    ]
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path.rstrip("/") or "/",
            urlencode(sorted(query)),
            "",
        )
    )


def _guid(key: str) -> str:
    return "article-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


# 以破折號接在標題後的站名長度上限；超過視為標題本身的一部分。
MAX_TITLE_SITE_CHARS = 20


def _title_and_site(raw_title: str, sitename: str, host: str) -> tuple[str, str]:
    """去掉網頁標題後綴的分類與站名，並決定媒體名稱。

    標題後綴有兩種寫法：「標題 | 兩岸 | 中央社 CNA」取第一段；
    「標題 - 報導者 The Reporter」只切最後一個破折號。
    網頁沒有 og:site_name 時 trafilatura 的 sitename 只是網域，
    此時改用標題後綴的站名；也沒有就用網域。

    Returns:
        (標題, 媒體名稱)。
    """
    title = raw_title.strip()
    sitename = sitename.strip()
    if not sitename or _is_host(sitename, host):
        sitename = ""

    suffix = ""
    parts = re.split(r"\s+[|｜]\s+", title)
    if len(parts) > 1:
        title, suffix = parts[0].strip(), parts[-1].strip()
    else:
        dashed = re.match(r"^(.*\S)\s+[-–—]\s+([^-–—]+)$", title)
        # 有真正站名時，破折號後綴要與站名相同才切，避免切到標題本身的「A - B」
        if dashed and len(dashed.group(2).strip()) <= MAX_TITLE_SITE_CHARS and (
            not sitename or dashed.group(2).strip() == sitename
        ):
            title, suffix = dashed.group(1).strip(), dashed.group(2).strip()

    return title, sitename or suffix or _bare_host(host)


def _is_host(name: str, host: str) -> bool:
    """trafilatura 抓不到站名時會以網域代替，如 twreporter.org。"""
    return name.lower() in {host.lower(), _bare_host(host).lower()}


def _split_paragraphs(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _bare_host(host: str) -> str:
    return host.removeprefix("www.")


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
