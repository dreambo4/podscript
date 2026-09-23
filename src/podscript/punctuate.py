"""以 claude -p 為逐字稿補上標點。

whisper.cpp 的中文輸出不含標點，本模組為選用的後處理工具，
未接入 pipeline，需要時自行呼叫 punctuate_segments。
每次執行會消耗 claude 訂閱額度（實測 1500 字約 70 秒）。

以 segment 為單位處理，時間軸與段落數維持不變。
"""
from __future__ import annotations

import re
import subprocess
import tempfile
import unicodedata
from pathlib import Path

from .transcribe import Segment

# 一次送入模型的字數上限。過長會增加漏段風險，過短則呼叫次數過多。
BATCH_CHARS = 2000

# 單批的嘗試次數。模型偶爾會少回或多回幾行，重試即可通過行數與文字檢查。
MAX_ATTEMPTS = 3

PROMPT = """讀取 {path}，為這段台灣中文 Podcast 逐字稿加上標點符號。

規則：
1. 只加標點，不得增加、刪除或修改任何一個字
2. 只使用這些全形標點：，。？！、
3. 不要使用破折號、引號、括號或其他符號
4. 保留口語中的語氣詞（啊、喔、欸等）
5. 直接輸出結果，不要任何說明文字或前後綴

每一行是獨立的一段，請逐行加標點，並保持行數與順序完全不變。"""


class PunctuateError(Exception):
    """補標點失敗。"""


def punctuate_segments(
    segments: list[Segment],
    *,
    model: str = "haiku",
    timeout: int = 300,
) -> list[Segment]:
    """為 segment 清單補上標點，時間軸與段落數不變。

    逐批送入模型；任一批失敗或行數對不上時，該批維持原文，不中斷整體流程。

    Args:
        model: claude CLI 的模型代號。
        timeout: 單批呼叫的逾時秒數。
    """
    result: list[Segment] = []
    for batch in _batches(segments):
        texts = [s.text for s in batch]
        punctuated = _punctuate_batch(texts, model=model, timeout=timeout)

        for seg, text in zip(batch, punctuated):
            result.append(
                Segment(
                    start=seg.start,
                    end=seg.end,
                    text=text,
                    avg_logprob=seg.avg_logprob,
                )
            )
    return result


def _punctuate_batch(
    texts: list[str], *, model: str, timeout: int, attempts: int = MAX_ATTEMPTS
) -> list[str]:
    """補一批標點，失敗則重試。全部失敗時回傳原文。

    模型偶爾會少回或多回幾行，重試通常可成功；
    連續失敗才放棄該批，以免整份逐字稿中斷。
    """
    for _ in range(attempts):
        try:
            punctuated = _call_claude(texts, model=model, timeout=timeout)
        except PunctuateError:
            continue

        if len(punctuated) == len(texts) and _text_preserved(texts, punctuated):
            return punctuated
    return texts


def _batches(segments: list[Segment]) -> list[list[Segment]]:
    """依字數切批。"""
    batches: list[list[Segment]] = []
    current: list[Segment] = []
    size = 0

    for seg in segments:
        if current and size + len(seg.text) > BATCH_CHARS:
            batches.append(current)
            current, size = [], 0
        current.append(seg)
        size += len(seg.text)

    if current:
        batches.append(current)
    return batches


def _call_claude(texts: list[str], *, model: str, timeout: int) -> list[str]:
    """把多行文字送給 claude CLI 補標點。

    逐字稿以檔案傳遞而非命令列參數，避免超過 ARG_MAX（spec §4.5）。

    Raises:
        PunctuateError: CLI 非零退出、逾時，或找不到 claude。
    """
    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", encoding="utf-8", delete=False
    ) as fh:
        fh.write("\n".join(texts))
        path = Path(fh.name)

    try:
        proc = subprocess.run(
            ["claude", "-p", PROMPT.format(path=path)],
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        raise PunctuateError(f"claude 補標點逾時（{timeout}s）") from exc
    except FileNotFoundError as exc:
        raise PunctuateError("找不到 claude CLI") from exc
    finally:
        path.unlink(missing_ok=True)

    if proc.returncode != 0:
        raise PunctuateError(f"claude 補標點失敗：{proc.stderr.strip()[:300]}")

    return [line.strip() for line in proc.stdout.strip().split("\n") if line.strip()]


def _text_preserved(original: list[str], punctuated: list[str]) -> bool:
    """確認除標點外文字完全未變，避免模型改寫內容。"""
    return _strip_marks("".join(original)) == _strip_marks("".join(punctuated))


def _strip_marks(text: str) -> str:
    """移除標點、空白與製表符號，只留下實際文字。"""
    cleaned = re.sub(r"[─—–\-]+", "", text)
    return "".join(
        c
        for c in cleaned
        if not unicodedata.category(c).startswith("P") and not c.isspace()
    )
