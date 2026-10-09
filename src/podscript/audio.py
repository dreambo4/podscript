"""音檔下載與前處理。

階段性快取以「檔案是否存在」判斷（spec §4.4），每個單集一個目錄：

    audio/<episode_guid>/
    ├── source.mp3
    ├── audio.wav        ← whisper.cpp 與 pyannote 共用的 16kHz 單聲道 WAV
    ├── whisper.json     ← 轉錄結果
    ├── diarize.json     ← 說話者時段
    ├── transcript.json  ← 合併後的逐字稿與中繼資料
    └── transcript.txt   ← 可讀版本，供下載與 claude CLI 讀取
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Callable

import requests

AUDIO_ROOT = Path(__file__).resolve().parents[2] / "audio"
CHUNK_SIZE = 1 << 20

# whisper.cpp 與 pyannote 皆以 16kHz 單聲道為輸入格式。
SAMPLE_RATE = 16000

ProgressFn = Callable[[int, int | None], None]


class AudioError(Exception):
    """下載或轉檔失敗。"""


def episode_dir(episode_guid: str) -> Path:
    """取得單集的工作目錄，不存在則建立。"""
    path = AUDIO_ROOT / _safe_name(episode_guid)
    path.mkdir(parents=True, exist_ok=True)
    return path


def download_mp3(
    episode_guid: str,
    fetch: Callable[[Path], None],
    *,
    force: bool = False,
) -> Path:
    """取得 source.mp3。已存在且未指定 force 時直接沿用。

    Args:
        fetch: 把 mp3 寫到指定路徑的函式，由各平台 resolver 提供
            （見 PlatformResolver.download），此處只負責快取與暫存檔。

    Raises:
        AudioError: 下載失敗，或下載內容為空。
    """
    target = episode_dir(episode_guid) / "source.mp3"
    if target.exists() and not force:
        return target

    # 寫入暫存檔，完成後才更名，避免中斷留下不完整的檔案被當成快取。
    partial = target.with_suffix(".mp3.part")
    try:
        fetch(partial)
    except AudioError:
        partial.unlink(missing_ok=True)
        raise

    if not partial.exists() or partial.stat().st_size == 0:
        partial.unlink(missing_ok=True)
        raise AudioError("下載到空檔案；音檔網址可能已失效")

    partial.replace(target)
    return target


def fetch_http(
    url: str, target: Path, *, on_progress: ProgressFn | None = None
) -> None:
    """以 HTTP 串流下載到 target。

    Args:
        on_progress: 收到 (已下載位元組, 總位元組或 None) 的回呼。

    Raises:
        AudioError: HTTP 失敗。
    """
    try:
        with requests.get(url, stream=True, timeout=60) as resp:
            resp.raise_for_status()
            total = resp.headers.get("Content-Length")
            total_bytes = int(total) if total and total.isdigit() else None

            downloaded = 0
            with target.open("wb") as fh:
                for chunk in resp.iter_content(CHUNK_SIZE):
                    fh.write(chunk)
                    downloaded += len(chunk)
                    if on_progress:
                        on_progress(downloaded, total_bytes)
    except requests.RequestException as exc:
        raise AudioError(f"下載音檔失敗：{exc}") from exc


def to_wav(
    mp3_path: Path,
    *,
    force: bool = False,
    start_sec: float | None = None,
    duration_sec: float | None = None,
    suffix: str = "",
) -> Path:
    """轉成 16kHz 單聲道 WAV。

    Args:
        start_sec: 起始秒數，用於節錄片段（如 §9.1 對照實測）。
        duration_sec: 擷取長度（秒）。
        suffix: 加在檔名後的識別字，供片段檔與全長檔並存。

    Raises:
        AudioError: 找不到 ffmpeg，或轉檔失敗。
    """
    if shutil.which("ffmpeg") is None:
        raise AudioError("找不到 ffmpeg，請先執行 brew install ffmpeg")

    target = mp3_path.with_name(f"audio{suffix}.wav")
    if target.exists() and not force:
        return target

    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    if start_sec is not None:
        cmd += ["-ss", str(start_sec)]
    cmd += ["-i", str(mp3_path)]
    if duration_sec is not None:
        cmd += ["-t", str(duration_sec)]
    # 先寫暫存檔再改名：轉檔中途被終止時不留下不完整的 wav，
    # 否則下次接續會因檔案存在而跳過轉檔，拿殘缺的音檔去轉錄。
    partial = target.with_name(f"audio{suffix}.partial.wav")
    cmd += ["-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", str(partial)]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        partial.unlink(missing_ok=True)
        raise AudioError(f"ffmpeg 轉檔失敗：{result.stderr.strip()[:500]}")
    partial.replace(target)
    return target


def _safe_name(value: str) -> str:
    """把 guid 轉成可用於目錄名稱的字串。"""
    cleaned = "".join(c if c.isalnum() or c in "-_" else "_" for c in value)
    return cleaned[:120] or "unknown"
