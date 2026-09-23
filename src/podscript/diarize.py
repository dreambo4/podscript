"""pyannote.audio 說話者分離，以及與 whisper segment 的時間軸對齊。

pyannote 只輸出編號（SPEAKER_00…），不知道說話者是誰；
編號在不同次執行間也不保證一致，故不做跨集辨識（spec §4.3）。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .transcribe import Segment

PIPELINE_NAME = "pyannote/speaker-diarization-3.1"


@dataclass
class SpeakerTurn:
    """一段連續的說話時間。"""

    start: float
    end: float
    speaker: str

    def to_dict(self) -> dict:
        return {"start": self.start, "end": self.end, "speaker": self.speaker}


@dataclass
class DiarizedSegment:
    """帶說話者的逐字稿片段。"""

    start: float
    end: float
    speaker: str
    text: str
    confidence: float

    def to_dict(self) -> dict:
        return {
            "start": round(self.start, 2),
            "end": round(self.end, 2),
            "speaker": self.speaker,
            "text": self.text,
            "confidence": round(self.confidence, 3),
        }


class DiarizeError(Exception):
    """說話者分離失敗。"""


def engine_version() -> str:
    """取得 pyannote.audio 版本，用於記錄說話者分離的產生環境。"""
    try:
        import pyannote.audio

        return pyannote.audio.__version__
    except (ImportError, AttributeError):
        return "unknown"


def diarize(
    wav_path: Path,
    *,
    output_path: Path | None = None,
    force: bool = False,
    num_speakers: int | None = None,
) -> list[SpeakerTurn]:
    """執行說話者分離，結果寫入 diarize.json。

    Args:
        num_speakers: 已知說話者人數時可指定，能提升分離準確度。

    Raises:
        DiarizeError: 缺少 HUGGINGFACE_TOKEN、模型載入失敗，或推論出錯。
    """
    target = output_path or wav_path.with_name("diarize.json")
    if target.exists() and not force:
        return load_turns(target)

    # 在子行程執行：torch 的 MPS 快取在推論結束後不會還給系統，
    # 留在常駐的服務行程裡會與下一集的 whisper 疊加，把記憶體吃光。
    # 子行程結束時作業系統會完整回收。
    cmd = [sys.executable, "-m", __name__, str(wav_path), str(target)]
    if num_speakers:
        cmd += ["--num-speakers", str(num_speakers)]
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        lines = proc.stderr.strip().splitlines()
        raise DiarizeError(lines[-1] if lines else f"說話者分離程序異常結束（{proc.returncode}）")
    return load_turns(target)


def _run(wav_path: Path, target: Path, num_speakers: int | None) -> None:
    """實際執行 pyannote 並寫出 diarize.json，僅在子行程中呼叫。"""
    token = os.environ.get("HUGGINGFACE_TOKEN")
    if not token:
        raise DiarizeError(
            "缺少 HUGGINGFACE_TOKEN；請至 https://huggingface.co/settings/tokens 建立，"
            "並先同意 pyannote/speaker-diarization-3.1 與 pyannote/segmentation-3.0 的條款"
        )

    # torch 與 pyannote 匯入成本高，僅在實際執行時載入。
    import torch
    from pyannote.audio import Pipeline

    try:
        pipeline = Pipeline.from_pretrained(PIPELINE_NAME, token=token)
    except Exception as exc:
        raise DiarizeError(
            f"載入 {PIPELINE_NAME} 失敗：{exc}；"
            "請確認 token 有效且已同意模型條款"
        ) from exc

    if torch.backends.mps.is_available():
        pipeline.to(torch.device("mps"))

    try:
        output = pipeline(str(wav_path), num_speakers=num_speakers)
    except Exception as exc:
        raise DiarizeError(f"說話者分離失敗：{exc}") from exc

    # pyannote 4.x 回傳 DiarizeOutput；取 exclusive 版本，
    # 它已移除重疊語音，正是逐字稿對齊所需（同一時點只屬於一位說話者）。
    annotation = output.exclusive_speaker_diarization

    turns = [
        SpeakerTurn(start=segment.start, end=segment.end, speaker=speaker)
        for segment, _, speaker in annotation.itertracks(yield_label=True)
    ]
    target.write_text(
        json.dumps([t.to_dict() for t in turns], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_turns(json_path: Path) -> list[SpeakerTurn]:
    """讀取已存在的 diarize.json。"""
    data = json.loads(json_path.read_text(encoding="utf-8"))
    return [SpeakerTurn(**item) for item in data]


def align(
    segments: list[Segment],
    turns: list[SpeakerTurn],
    *,
    default_speaker: str = "SPEAKER_00",
) -> list[DiarizedSegment]:
    """把說話者標記套到逐字稿片段上。

    以時間軸重疊長度最大者為該片段的說話者；
    完全沒有重疊時（pyannote 未涵蓋的區段）沿用 default_speaker。
    """
    result: list[DiarizedSegment] = []
    for seg in segments:
        speaker = _dominant_speaker(seg, turns) or default_speaker
        result.append(
            DiarizedSegment(
                start=seg.start,
                end=seg.end,
                speaker=speaker,
                text=seg.text,
                confidence=seg.avg_logprob if seg.avg_logprob is not None else 1.0,
            )
        )
    return result


def _dominant_speaker(segment: Segment, turns: list[SpeakerTurn]) -> str | None:
    """找出與該片段重疊時間最長的說話者。"""
    overlaps: dict[str, float] = {}
    for turn in turns:
        overlap = min(segment.end, turn.end) - max(segment.start, turn.start)
        if overlap > 0:
            overlaps[turn.speaker] = overlaps.get(turn.speaker, 0.0) + overlap

    if not overlaps:
        return None
    return max(overlaps, key=overlaps.get)


def merge_adjacent(
    segments: list[DiarizedSegment], *, max_gap: float = 1.0
) -> list[DiarizedSegment]:
    """合併同一說話者的連續片段，避免逐句換行過於零碎。

    Args:
        max_gap: 兩片段間隔超過此秒數則不合併，保留語氣停頓。
    """
    if not segments:
        return []

    merged = [segments[0]]
    for seg in segments[1:]:
        last = merged[-1]
        if seg.speaker == last.speaker and seg.start - last.end <= max_gap:
            merged[-1] = DiarizedSegment(
                start=last.start,
                end=seg.end,
                speaker=last.speaker,
                text=last.text + seg.text,
                confidence=min(last.confidence, seg.confidence),
            )
        else:
            merged.append(seg)
    return merged


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="執行說話者分離（供 diarize() 以子行程呼叫）")
    parser.add_argument("wav", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--num-speakers", type=int)
    args = parser.parse_args()
    try:
        _run(args.wav, args.output, args.num_speakers)
    except DiarizeError as exc:
        # 父行程取 stderr 最後一行作為錯誤訊息。
        print(exc, file=sys.stderr)
        sys.exit(1)
