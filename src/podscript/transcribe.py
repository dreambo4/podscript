"""whisper.cpp 轉錄。

輸出 whisper.json，格式為 segment 清單，每段含起訖秒數、文字與平均 logprob。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import hardware

MODELS_DIR = Path(__file__).resolve().parents[2] / "models"
WHISPER_BIN = "whisper-cli"

# whisper-cli --print-progress 的輸出格式：progress = 42%
_PROGRESS_RE = re.compile(r"progress\s*=\s*(\d+)%")

# 繁體引導句。Whisper 只有單一 zh 標籤，不給提示時會隨機輸出簡體。
# 內容同時作為中英夾雜的示範，降低英文詞被硬轉成中文的機率。
ZH_TW_PROMPT = "以下是繁體中文的 Podcast 對談逐字稿，包含台灣華語口語與少量英文詞彙，例如 case、confirm、podcast。"


def build_prompt(title: str = "") -> str:
    """組出 initial_prompt，附上單集標題。

    注意：實測 whisper-cli 的 --prompt 對 large-v2 的中文輸出沒有作用
    （改用簡體引導句，輸出仍為純繁體且一字不變）。
    保留此函式是為了在 whisper.cpp 修正該行為後能立即生效。

    Args:
        title: 單集標題。
    """
    if not title.strip():
        return ZH_TW_PROMPT
    return f"{ZH_TW_PROMPT}本集標題：{title.strip()}"


@dataclass
class Segment:
    start: float
    end: float
    text: str
    avg_logprob: float | None = None

    def to_dict(self) -> dict:
        return {
            "start": self.start,
            "end": self.end,
            "text": self.text,
            "avg_logprob": self.avg_logprob,
        }


class TranscribeError(Exception):
    """轉錄失敗。"""


def engine_version() -> str:
    """取得 whisper.cpp 版本，用於記錄逐字稿的產生環境。

    版本取不到時回傳 unknown，不影響轉錄流程。
    """
    result = subprocess.run(
        ["brew", "list", "--versions", "whisper.cpp"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return "unknown"

    parts = result.stdout.split()
    return parts[-1] if len(parts) >= 2 else "unknown"


def model_path(model: str) -> Path:
    """取得 ggml 模型檔路徑。

    Args:
        model: 模型名稱，如 large-v3。

    Raises:
        TranscribeError: 模型檔不存在。
    """
    path = MODELS_DIR / f"ggml-{model}.bin"
    if not path.exists():
        raise TranscribeError(
            f"找不到模型 {path}；請執行 python3 scripts/setup-model.py 下載"
        )
    return path


def transcribe(
    wav_path: Path,
    *,
    model: str = "large-v2",
    output_path: Path | None = None,
    force: bool = False,
    threads: int | None = None,
    initial_prompt: str = ZH_TW_PROMPT,
    on_percent: Callable[[int], None] | None = None,
) -> list[Segment]:
    """轉錄 WAV，回傳 segment 清單並寫出 JSON。

    以 --max-context 0 執行，不將前文帶入 decoder，
    避免模型在靜音段落沿著前文無限重複同一句。

    Args:
        model: ggml 模型名稱，需有對應的 models/ggml-<model>.bin。
        output_path: 輸出位置，預設為音檔同目錄的 whisper.json。
        force: 已有輸出檔時仍重新轉錄。
        threads: 執行緒數，省略則依本機效能核心數。

    Raises:
        TranscribeError: 找不到 whisper-cli 或模型、轉錄程序非零退出、輸出無法解析。
    """
    if shutil.which(WHISPER_BIN) is None:
        raise TranscribeError(f"找不到 {WHISPER_BIN}，請先執行 brew install whisper-cpp")

    target = output_path or wav_path.with_name("whisper.json")
    if target.exists() and not force:
        return load_segments(target)

    path = model_path(model)

    # whisper-cli 會自動補上 .json 副檔名，因此傳入去掉副檔名的前綴。
    prefix = target.with_suffix("")
    cmd = [
        WHISPER_BIN,
        "-m", str(path),
        "-f", str(wav_path),
        "-l", "zh",
        "--prompt", initial_prompt,
        "--max-context", "0",
        "--suppress-nst",
        "--output-json-full",
        "--output-file", str(prefix),
        "--threads", str(threads or hardware.performance_cores()),
        "--print-progress",
    ]

    stderr = _run_with_progress(cmd, on_percent)
    if stderr is not None:
        raise TranscribeError(f"whisper-cli 轉錄失敗：{stderr[-800:]}")

    produced = prefix.with_suffix(".json")
    if not produced.exists():
        raise TranscribeError(f"whisper-cli 未產生輸出檔 {produced}")
    if produced != target:
        produced.replace(target)

    return load_segments(target)


def _run_with_progress(
    cmd: list[str], on_percent: Callable[[int], None] | None
) -> str | None:
    """執行 whisper-cli 並即時回報進度。

    whisper-cli 的 --print-progress 會把 "whisper_print_progress_callback:
    progress = 42%" 寫到 stderr，逐行讀取才能在轉錄期間更新畫面；
    一次 capture_output 要等程序結束才拿得到，20 分鐘內畫面會完全靜止。

    Returns:
        成功為 None，失敗則回傳 stderr 內容。
    """
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    tail: list[str] = []
    last = -1
    for line in process.stderr:
        tail.append(line)
        del tail[:-40]  # 失敗時只需要末尾訊息

        match = _PROGRESS_RE.search(line)
        if match and on_percent:
            percent = int(match.group(1))
            if percent != last:
                last = percent
                on_percent(percent)

    process.wait()
    return None if process.returncode == 0 else "".join(tail).strip()


def load_segments(json_path: Path) -> list[Segment]:
    """讀取 whisper.cpp 的 JSON 輸出。

    Raises:
        TranscribeError: JSON 格式非預期。
    """
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise TranscribeError(f"讀取 {json_path.name} 失敗：{exc}") from exc

    raw_segments = data.get("transcription")
    if raw_segments is None:
        raise TranscribeError(f"{json_path.name} 中找不到 transcription 欄位")

    segments: list[Segment] = []
    for item in raw_segments:
        offsets = item.get("offsets") or {}
        text = (item.get("text") or "").strip()
        if not text:
            continue
        segments.append(
            Segment(
                start=offsets.get("from", 0) / 1000,
                end=offsets.get("to", 0) / 1000,
                text=text,
                avg_logprob=_avg_logprob(item),
            )
        )
    return segments


def _avg_logprob(item: dict) -> float | None:
    """從 token 層資料計算平均 logprob，作為信心度依據。"""
    tokens = item.get("tokens")
    if not tokens:
        return None

    values = [t["p"] for t in tokens if isinstance(t.get("p"), (int, float))]
    if not values:
        return None
    return sum(values) / len(values)
