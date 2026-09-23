"""逐字稿後處理。

提供四項獨立功能：繁體轉換、幻覺句偵測、跳針重複偵測、信心度換算。
呼叫端自行決定要套用哪些（pipeline 會濾掉幻覺與跳針段落）。
"""
from __future__ import annotations

import re

from opencc import OpenCC

# s2tw 轉出台灣標準字形（才／為／裡／著），不改用詞。
# 勿改為 s2twp，其片語表會破壞術語（實價登錄→實價登入、社區→社群）。
_converter = OpenCC("s2tw")

# 幻覺句黑名單。Whisper 在靜音或音樂段落會生成字幕常見語，
# 這些句子在 Podcast 對談中幾乎不可能自然出現。
HALLUCINATION_PATTERNS = [
    r"^請訂閱",
    r"^謝謝(大家的)?觀看",
    r"^謝謝收看",
    r"字幕(由|志願者|組)",
    r"独播剧场|獨播劇場",
    r"YoYo Television",
    r"^請不吝",
    r"^點贊",
    r"^本字幕",
    r"MING PAO|明鏡",
]

_hallucination_re = re.compile("|".join(HALLUCINATION_PATTERNS))


def to_traditional(text: str) -> str:
    """轉為台灣正體。"""
    return _converter.convert(text)


def is_hallucination(text: str) -> bool:
    """判斷是否為字幕語幻覺。"""
    return bool(_hallucination_re.search(text.strip()))


def is_repetitive(text: str, *, min_repeats: int = 4) -> bool:
    """偵測單句內的無限重複。

    Whisper 跳針時會把同一短語連續重複數十次，特徵是短字串高頻出現。

    Args:
        min_repeats: 同一短語重複幾次以上視為異常。
    """
    stripped = re.sub(r"\s+", "", text)
    if len(stripped) < 12:
        return False

    # 檢查 2 到 8 字的短語是否連續重複
    for size in range(2, 9):
        unit = stripped[:size]
        repeated = unit * min_repeats
        if stripped.startswith(repeated):
            return True
    return False


def confidence_of(avg_logprob: float | None) -> float:
    """把 whisper 的 token 機率均值轉為 0-1 的信心度。

    whisper.cpp 的 output-json-full 已提供機率值而非對數機率，
    此處僅做範圍箝制，保留欄位語意供未來換算方式調整。
    """
    if avg_logprob is None:
        return 1.0
    return max(0.0, min(1.0, avg_logprob))
