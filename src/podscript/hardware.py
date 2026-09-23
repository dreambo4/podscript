"""本機硬體偵測，決定這台機器該用的轉錄模型與執行緒數。

同一套程式可能跑在記憶體 8GB 與 16GB 以上的 Mac 上。whisper 模型整份載入
記憶體（Metal 緩衝區無法換出），選錯模型會吃光記憶體讓整台機器卡死。

機器規格固定，模型只需在安裝時決定一次（scripts/setup-model.py 寫入
.env 的 WHISPER_MODEL），執行時不再判斷。
"""
from __future__ import annotations

import os
import subprocess

GB = 1024**3

# 各模型轉錄時的記憶體峰值（GB），以 /usr/bin/time -l 實測 peak memory footprint。
# 僅供選模型時參考。
MODEL_PEAK_GB = {
    "large-v2": 4.0,
    "large-v2-q5_0": 2.0,
}

# 實體記憶體達此值才用完整版 large-v2；以下改用量化版。
# large-v2 峰值 4GB，加上系統與日常程式約需 8GB。
FULL_MODEL_MIN_TOTAL_GB = 12


def total_memory() -> int:
    """實體記憶體總量（bytes）。"""
    return int(_sysctl("hw.memsize") or 0)


def performance_cores() -> int:
    """效能核心數。

    Apple Silicon 分效能核心與節能核心，whisper 的執行緒排到節能核心上
    反而拖慢整體速度，故以效能核心數為準；取不到時退回總核心數。
    """
    value = _sysctl("hw.perflevel0.physicalcpu")
    if value and value.isdigit():
        return int(value)
    return os.cpu_count() or 4


def recommended_model() -> str:
    """依實體記憶體推薦這台機器的轉錄模型。"""
    if total_memory() >= FULL_MODEL_MIN_TOTAL_GB * GB:
        return "large-v2"
    return "large-v2-q5_0"


def _sysctl(name: str) -> str | None:
    try:
        return subprocess.run(
            ["sysctl", "-n", name], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
