#!/usr/bin/env python3
"""依本機規格選定轉錄模型，寫入 .env 並下載該模型。

機器規格固定，換到新電腦時執行一次即可；只下載選定的那一個模型。
.env 已有 WHISPER_MODEL 時會覆寫。

用法：
    python3 scripts/setup-model.py           # 依記憶體自動選擇
    python3 scripts/setup-model.py large-v2  # 手動指定
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from podscript import hardware  # noqa: E402
from podscript.transcribe import MODELS_DIR  # noqa: E402

DOWNLOAD_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-{model}.bin"
ENV_PATH = ROOT / ".env"


def main() -> None:
    memory_gb = hardware.total_memory() / hardware.GB
    model = sys.argv[1] if len(sys.argv) > 1 else hardware.recommended_model()
    peak = hardware.MODEL_PEAK_GB.get(model)

    print(f"實體記憶體 {memory_gb:.0f}GB，效能核心 {hardware.performance_cores()} 顆")
    print(f"轉錄模型：{model}" + (f"（記憶體峰值約 {peak:.1f}GB）" if peak else ""))

    download(model)
    write_env(model)
    print(f"已寫入 {ENV_PATH.name}：WHISPER_MODEL={model}；請重啟服務（./run.sh）")


def download(model: str) -> None:
    """下載模型；已存在則略過。先寫到 .part，完成才改名，避免中斷後留下殘檔。"""
    target = MODELS_DIR / f"ggml-{model}.bin"
    if target.exists():
        print(f"模型已存在：{target}")
        return

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(".bin.part")
    print(f"下載 {target.name} …")
    subprocess.run(
        ["curl", "-fL", "--progress-bar", "-o", str(partial), DOWNLOAD_URL.format(model=model)],
        check=True,
    )
    partial.replace(target)


def write_env(model: str) -> None:
    """設定 .env 的 WHISPER_MODEL，保留其他設定不動。"""
    line = f"WHISPER_MODEL={model}"
    text = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""
    if re.search(r"^WHISPER_MODEL=.*$", text, re.M):
        text = re.sub(r"^WHISPER_MODEL=.*$", line, text, flags=re.M)
    else:
        text = text.rstrip("\n") + ("\n" if text else "") + line + "\n"
    ENV_PATH.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
