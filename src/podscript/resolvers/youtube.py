"""YouTube 網址解析與音訊下載。

YouTube 沒有固定的音檔直連（串流網址帶簽章且會過期），解析與下載都交給 yt-dlp。
只下載純音軌再轉成 mp3，不下載影像。

yt-dlp 需要 JavaScript 執行環境才能通過 YouTube 的驗證，
依序使用 deno、node 中本機有的那一個。
YouTube 改版常讓舊版 yt-dlp 失效，下載失敗時先升級：pip install -U "yt-dlp[default]"
"""
from __future__ import annotations

import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yt_dlp

from ..audio import AudioError
from .base import Episode, PlatformResolver, ResolveError

_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
# 路徑形式的影片網址：/live/<id>、/shorts/<id>、/embed/<id>
_PATH_PREFIXES = ("live", "shorts", "embed")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

MP3_QUALITY = "128"


class YouTubeResolver(PlatformResolver):
    platform = "youtube"

    def can_handle(self, url: str) -> bool:
        return (urlparse(url).hostname or "").lower() in _HOSTS

    def resolve(self, url: str) -> Episode:
        video_id = _parse_video_id(url)
        # 以標準網址查詢：去掉播放清單參數，避免 yt-dlp 展開整個清單，
        # 也去掉分享追蹤參數（si）。
        canonical = _canonical_url(video_id)
        try:
            with yt_dlp.YoutubeDL(_base_options()) as ydl:
                info = ydl.extract_info(canonical, download=False)
        except yt_dlp.utils.DownloadError as exc:
            raise ResolveError(f"YouTube 解析失敗：{_clean_error(exc)}") from exc

        live_status = info.get("live_status")
        if live_status in ("is_live", "is_upcoming", "post_live"):
            raise ResolveError("直播尚未結束或仍在處理中，請等直播存檔完成後再試")

        duration = info.get("duration")
        return Episode(
            platform=self.platform,
            source_url=canonical,
            episode_guid=f"yt_{video_id}",
            podcast_name=info.get("channel") or info.get("uploader") or "",
            title=info.get("title") or "",
            mp3_url="",
            duration_sec=round(duration) if duration else None,
            published_at=_published_at(info),
            description=info.get("description") or "",
        )

    def download(self, episode: Episode, target: Path) -> None:
        """只下載純音軌並轉成 mp3。

        yt-dlp 會自行產生中間檔（原始音軌、轉檔暫存），放在同目錄下的暫存資料夾，
        完成後只把 mp3 移到 target。
        """
        with tempfile.TemporaryDirectory(dir=target.parent, prefix="ytdlp-") as tmp:
            options = _base_options() | {
                # 只選純音軌格式；沒有時寧可失敗，也不下載含影像的格式。
                "format": "bestaudio",
                "outtmpl": str(Path(tmp) / "audio.%(ext)s"),
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": MP3_QUALITY,
                    }
                ],
            }
            try:
                with yt_dlp.YoutubeDL(options) as ydl:
                    ydl.download([episode.source_url])
            except yt_dlp.utils.DownloadError as exc:
                raise AudioError(f"YouTube 下載失敗：{_clean_error(exc)}") from exc

            produced = Path(tmp) / "audio.mp3"
            if not produced.exists():
                raise AudioError("YouTube 下載完成但找不到轉出的 mp3")
            produced.replace(target)


def _parse_video_id(url: str) -> str:
    """從各種 YouTube 網址形式取出 11 碼影片 id。"""
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    parts = [p for p in parsed.path.split("/") if p]

    candidate = None
    if host == "youtu.be":
        candidate = parts[0] if parts else None
    elif parts[:1] == ["watch"]:
        candidate = parse_qs(parsed.query).get("v", [None])[0]
    elif len(parts) >= 2 and parts[0] in _PATH_PREFIXES:
        candidate = parts[1]

    if not candidate or not _VIDEO_ID.match(candidate):
        raise ResolveError(
            "網址中找不到影片 id；請複製單支影片的網址，而非頻道或播放清單頁面"
        )
    return candidate


def _canonical_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def _base_options() -> dict:
    return {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "js_runtimes": _js_runtimes(),
    }


def _js_runtimes() -> dict:
    """選用本機已有的 JavaScript 執行環境；都沒有時交給 yt-dlp 的預設值。"""
    for name in ("deno", "node"):
        if shutil.which(name):
            return {name: {}}
    return {"deno": {}}


def _published_at(info: dict) -> datetime | None:
    """優先用精確的上傳時間戳，沒有時退回只有日期的 upload_date。"""
    timestamp = info.get("timestamp") or info.get("release_timestamp")
    if timestamp:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)

    upload_date = info.get("upload_date")
    if upload_date:
        try:
            return datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _clean_error(exc: Exception) -> str:
    """去掉 yt-dlp 錯誤訊息的色碼與 ERROR 前綴，讓訊息可直接顯示給使用者。"""
    message = _ANSI.sub("", str(exc))
    message = re.sub(r"^ERROR:\s*", "", message)
    if "javascript" in message.lower() or "challenge" in message.lower():
        message += "（可能是 yt-dlp 版本過舊或找不到 node/deno，請升級 yt-dlp 並確認 PATH）"
    return message
