"""單集處理流程。

串接解析、下載、轉錄、說話者分離與對齊，產出 transcript.json 與 transcript.txt。
各階段以「檔案是否存在」判斷是否已完成（spec §4.4）；
process() 的 force 參數可指定從哪個階段起重跑。
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import audio, chapters, diarize, postprocess, summary, transcribe
from . import resolvers
from .resolvers import Episode, resolve
from .resolvers.article import PLATFORM as ARTICLE_PLATFORM, Article

STAGES = ("download", "transcribe", "diarize", "merge", "summarize")

# 文章沒有音檔，只有摘要一個階段。
ARTICLE_STAGES = ("summarize",)

# (階段, 訊息, 百分比)。百分比僅轉錄階段有值，其餘為 None。
ProgressFn = Callable[[str, str, "int | None"], None]


@dataclass
class Provenance:
    """記錄各階段由哪個模型產生，供日後比對版本間的品質差異。

    Attributes:
        transcribe_model: whisper 模型名稱，如 large-v2。
        transcribe_engine: whisper.cpp 版本。
        diarize_model: pyannote pipeline 名稱。
        diarize_engine: pyannote.audio 套件版本。
        summary_model: 產生摘要、心智圖與標籤的模型 ID。三項由同一次
            claude -p 呼叫產生，共用此欄位。
        generated_at: 摘要階段的生成時間，可重新生成而更新。
    """

    transcribe_model: str = ""
    transcribe_engine: str = ""
    diarize_model: str = ""
    diarize_engine: str = ""
    summary_model: str = ""
    generated_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Result:
    """單集處理結果。"""

    episode: Episode
    segments: list[diarize.DiarizedSegment]
    speakers: dict[str, str]
    provenance: Provenance

    def to_dict(self) -> dict:
        return {
            "episode": self.episode.to_dict(),
            "segments": [s.to_dict() for s in self.segments],
            "speakers": self.speakers,
            "provenance": self.provenance.to_dict(),
        }


def process(
    url: str,
    *,
    model: str = "large-v2",
    force: str | None = None,
    num_speakers: int | None = None,
    summary_model: str | None = None,
    on_progress: ProgressFn | None = None,
) -> Result:
    """處理單集：解析 → 下載 → 轉錄 → 說話者分離 → 合併 → 摘要。

    Args:
        force: 強制重跑的階段名稱，見 STAGES。該階段之後的階段一併重跑。
        num_speakers: 已知說話者人數時可指定，能提升分離準確度。
        summary_model: 摘要模型，省略則讀 CLAUDE_CLI_MODEL。
    """
    notify = on_progress or (lambda stage, message, percent=None: None)
    forced = _forced_stages(force)

    notify("resolve", "解析網址", None)
    episode = resolve(url)

    notify("download", f"下載音檔：{episode.title}", None)
    mp3 = audio.download_mp3(
        episode.episode_guid,
        lambda target: resolvers.download(episode, target),
        force="download" in forced,
    )
    wav = audio.to_wav(mp3, force="download" in forced)

    notify("transcribe", "語音轉文字", 0)
    segments = transcribe.transcribe(
        wav,
        model=model,
        force="transcribe" in forced,
        initial_prompt=transcribe.build_prompt(episode.title),
        on_percent=lambda pct: notify("transcribe", "語音轉文字", pct),
    )
    segments = [
        transcribe.Segment(
            start=s.start,
            end=s.end,
            text=postprocess.to_traditional(s.text),
            avg_logprob=s.avg_logprob,
        )
        for s in segments
        if not postprocess.is_hallucination(s.text)
        and not postprocess.is_repetitive(s.text)
    ]

    notify("diarize", "說話者分離", None)
    turns = diarize.diarize(
        wav, force="diarize" in forced, num_speakers=num_speakers
    )

    notify("merge", "對齊時間軸", None)
    diarized = diarize.merge_adjacent(diarize.align(segments, turns))

    directory = mp3.parent
    speakers = _load_speakers(directory)
    result = Result(
        episode=episode,
        segments=diarized,
        speakers=speakers,
        provenance=Provenance(
            transcribe_model=model,
            transcribe_engine=f"whisper.cpp {transcribe.engine_version()}",
            diarize_model=diarize.PIPELINE_NAME,
            diarize_engine=f"pyannote.audio {diarize.engine_version()}",
        ),
    )
    _write_outputs(directory, result)

    notify("summarize", "產生摘要、心智圖與標籤", None)
    summarize(
        directory,
        result=result,
        force="summarize" in forced,
        summary_model=summary_model,
    )
    return result


def process_article(
    article: Article,
    *,
    force: bool = False,
    summary_model: str | None = None,
    on_progress: ProgressFn | None = None,
) -> Result:
    """處理文章：寫出正文後直接產生摘要，不經下載、轉錄與說話者分離。

    每段正文存成一個片段（時間為 0、無說話者），
    與逐字稿共用同一份資料格式，上傳、搜尋與重新生成都不必另外處理。

    Args:
        force: 已有摘要時是否重新生成。
    """
    notify = on_progress or (lambda stage, message, percent=None: None)
    episode = article.episode
    directory = audio.episode_dir(episode.episode_guid)

    result = Result(
        episode=episode,
        segments=[
            diarize.DiarizedSegment(
                start=0.0, end=0.0, speaker="", text=text, confidence=1.0
            )
            for text in article.paragraphs
        ],
        speakers={},
        provenance=Provenance(),
    )
    _write_outputs(directory, result)

    notify("summarize", "產生摘要、心智圖與標籤", None)
    summarize(
        directory, result=result, force=force, summary_model=summary_model
    )
    return result


def is_article(episode: Episode) -> bool:
    return episode.platform == ARTICLE_PLATFORM


def summarize(
    directory: Path,
    *,
    result: Result | None = None,
    force: bool = False,
    summary_model: str | None = None,
) -> summary.Summary | None:
    """產生摘要、心智圖、標籤、章節與封面。

    五項由同一次呼叫產生（見 summary 模組）；章節依段落時間檢查與對齊後才寫入。已有結果且未指定 force 時沿用，
    供「重新生成」按鈕在不重跑轉錄的情況下單獨呼叫。

    失敗時回傳 None 並保留既有逐字稿：摘要是附加價值，
    不應讓已完成的轉錄成果無法使用。

    Args:
        directory: 單集的工作目錄。
        result: 已載入的處理結果。傳入時逐字稿由它即時產生，
            不需要目錄中存有 transcript.txt，供從資料庫還原的單集使用；
            同時一併寫出 transcript.json，讓後續上傳能取得說話者名稱。
    """
    target = directory / "result.json"
    if target.exists() and not force:
        return None

    with _summary_transcript(directory, result) as transcript:
        model = summary_model or os.environ.get("CLAUDE_CLI_MODEL", "opus")
        provider = summary.get_provider(
            os.environ.get("SUMMARY_PROVIDER", "claude_cli")
        )
        try:
            generated = provider.generate(
                transcript, model=model, kind=_content_kind(directory, result)
            )
        except summary.SummaryError:
            return None

    generated.chapters = _normalize_chapters(
        generated.chapters, result or load_result(directory)
    )

    # 延遲匯入避免與 upload 模組的循環參照（upload 匯入 pipeline 取得 Result）。
    from . import upload as _upload

    # 合併建議待使用者確認，確認前 hashtags 維持原始標籤，不自動合併。
    generated.hashtags_generated = list(generated.hashtags)
    generated.hashtag_merges = summary.find_hashtag_merges(
        generated.hashtags, _upload.fetch_existing_hashtags(exclude_guid=directory.name)
    )

    directory.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(generated.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if result is not None:
        result.provenance.summary_model = generated.model
        result.provenance.generated_at = datetime.now(timezone.utc).isoformat()
        _write_outputs(directory, result)
    return generated


def add_chapters(
    directory: Path, result: Result, *, summary_model: str | None = None
) -> list[dict]:
    """只產生章節，不動摘要、心智圖與標籤。供已有摘要的舊集數補上章節。

    本機有 result.json 時一併寫入；已上傳者由呼叫端寫回資料庫。

    Returns:
        檢查與對齊後的章節，見 chapters.normalize。

    Raises:
        summary.SummaryError: 生成失敗，或模型回傳的章節全部不合格。
    """
    with _summary_transcript(directory, result) as transcript:
        model = summary_model or os.environ.get("CLAUDE_CLI_MODEL", "opus")
        provider = summary.get_provider(
            os.environ.get("SUMMARY_PROVIDER", "claude_cli")
        )
        raw = provider.generate_chapters(
            transcript, model=model, kind=_content_kind(directory, result)
        )

    normalized = _normalize_chapters(raw, result)
    if not normalized:
        raise summary.SummaryError("模型回傳的章節格式不正確，請再試一次")

    data = load_summary(directory)
    if data is not None:
        data["chapters"] = normalized
        (directory / "result.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return normalized


def regenerate_cover(
    directory: Path, result: Result, summary_data: dict, *, summary_model: str | None = None
) -> dict:
    """只重畫封面，不動摘要、心智圖、標籤與章節。

    依標題、摘要與標籤畫，不讀逐字稿。本機有 result.json 時一併寫入；
    已上傳者由呼叫端寫回資料庫。

    Returns:
        過濾後的封面 {"svg", "color"}，見 cover.normalize。

    Raises:
        summary.SummaryError: 生成失敗，或模型回傳的封面不可用。
    """
    model = summary_model or os.environ.get("CLAUDE_CLI_MODEL", "opus")
    provider = summary.get_provider(os.environ.get("SUMMARY_PROVIDER", "claude_cli"))
    cover = provider.generate_cover(
        title=result.episode.title,
        summary=summary_data.get("summary") or "",
        hashtags=summary_data.get("hashtags") or [],
        model=model,
    )

    data = load_summary(directory)
    if data is not None:
        data["cover"] = cover
        (directory / "result.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return cover


def _normalize_chapters(raw: list, result: Result | None) -> list[dict]:
    """檢查章節並對齊到段落：Podcast 依時間，文章依段落編號。"""
    if result is None:
        return []
    if is_article(result.episode):
        return chapters.normalize_paragraphs(raw, len(result.segments))
    return chapters.normalize(
        raw,
        [seg.start for seg in result.segments],
        duration=result.episode.duration_sec,
    )


@contextmanager
def _summary_transcript(directory: Path, result: Result | None):
    """提供供 claude CLI 讀取的逐字稿檔案。

    逐字稿不可當命令列參數傳遞（長度會超過 ARG_MAX），必須寫成檔案。
    目錄中沒有 transcript.txt 時（單集已上傳並清除），由 result 重建一份
    暫存檔，用畢刪除，不在已清空的目錄留下殘留。
    文章一律由 result 重建，確保段落編號（見 format_text）為最新格式。
    """
    existing = directory / "transcript.txt"
    rebuild_article = result is not None and is_article(result.episode)
    if result is None or (existing.exists() and not rebuild_article):
        yield existing
        return

    directory.mkdir(parents=True, exist_ok=True)
    rebuilt = directory / "transcript.summary.txt"
    rebuilt.write_text(format_text(result), encoding="utf-8")
    try:
        yield rebuilt
    finally:
        rebuilt.unlink(missing_ok=True)
        # 目錄可能是為了這次重建而建立，沒有其他檔案就一併移除。
        if not any(directory.iterdir()):
            directory.rmdir()


def load_result(directory: Path) -> Result | None:
    """讀取既有的處理結果，供介面開啟已處理過的單集。"""
    path = directory / "transcript.json"
    if not path.exists():
        return None

    data = json.loads(path.read_text(encoding="utf-8"))
    episode_data = dict(data["episode"])
    published = episode_data.get("published_at")
    if published:
        episode_data["published_at"] = datetime.fromisoformat(published)

    return Result(
        episode=Episode(**episode_data),
        segments=[
            diarize.DiarizedSegment(**seg) for seg in data.get("segments", [])
        ],
        speakers=data.get("speakers", {}),
        provenance=Provenance(**data.get("provenance", {})),
    )


def load_summary(directory: Path) -> dict | None:
    """讀取既有的摘要、心智圖與標籤。"""
    path = directory / "result.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def decide_hashtag_merges(directory: Path, decisions: dict[str, str]) -> dict:
    """套用使用者對標籤合併建議的決定並寫回 result.json。

    Raises:
        FileNotFoundError: 該目錄沒有摘要結果。
        ValueError: 決定內容不符合合併建議，見 summary.decide_hashtag_merges。
    """
    data = load_summary(directory)
    if data is None:
        raise FileNotFoundError(directory / "result.json")
    summary.decide_hashtag_merges(data, decisions)
    (directory / "result.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return data


def rename_speakers(directory: Path, speakers: dict[str, str]) -> Result:
    """更新說話者名稱並重寫輸出檔。

    名稱只影響顯示，不動逐字稿內容，故不需重跑任何階段。

    Raises:
        FileNotFoundError: 該目錄沒有處理結果。
    """
    result = load_result(directory)
    if result is None:
        raise FileNotFoundError(f"{directory} 沒有可更新的處理結果")

    # 空字串視為取消自訂，回到預設編號。
    result.speakers = {k: v.strip() for k, v in speakers.items() if v.strip()}
    _write_outputs(directory, result)
    return result


def _load_speakers(directory: Path) -> dict[str, str]:
    """沿用先前設定的說話者名稱，避免重跑時遺失。"""
    existing = load_result(directory)
    return existing.speakers if existing else {}


def _forced_stages(force: str | None) -> set[str]:
    """指定階段與其後續階段都需重跑，避免沿用過期的中間產物。"""
    if not force:
        return set()
    if force not in STAGES:
        raise ValueError(f"未知的階段 {force}；可用：{', '.join(STAGES)}")
    return set(STAGES[STAGES.index(force) :])


def _write_outputs(directory: Path, result: Result) -> None:
    """寫出供檢視與後續摘要使用的檔案。"""
    (directory / "transcript.json").write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (directory / "transcript.txt").write_text(format_text(result), encoding="utf-8")


def format_text(result: Result) -> str:
    """輸出供 claude CLI 讀取的全文：文章為標題加段落，其餘為逐字稿。

    文章每段開頭加 [n] 編號（1 起算），供模型回傳章節從第幾段開始。
    """
    if is_article(result.episode):
        paragraphs = [f"[{i}] {s.text}" for i, s in enumerate(result.segments, 1)]
        # 貼上全文未填標題時，標題就是第一段開頭，不重複列出
        first = result.segments[0].text if result.segments else ""
        if not first.startswith(result.episode.title):
            paragraphs.insert(0, result.episode.title)
        return "\n\n".join(paragraphs)
    return format_transcript(result.segments, result.speakers)


def _content_kind(directory: Path, result: Result | None) -> str:
    """摘要提示的內容類型；未傳入 result 時讀目錄中的處理結果判斷。"""
    loaded = result or load_result(directory)
    return "article" if loaded and is_article(loaded.episode) else "podcast"


def format_transcript(
    segments: list[diarize.DiarizedSegment], speakers: dict[str, str]
) -> str:
    """輸出可讀的逐字稿文字，供 claude CLI 讀取與使用者下載。"""
    lines = []
    for seg in segments:
        name = speakers.get(seg.speaker, seg.speaker)
        lines.append(f"[{_timestamp(seg.start)}] {name}\n{seg.text}\n")
    return "\n".join(lines)


def _timestamp(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"
