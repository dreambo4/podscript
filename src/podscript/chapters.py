"""章節的檢查與對齊。

模型給的章節時間是讀逐字稿上的時間戳推出來的，可能格式錯誤、超出節目長度
或順序顛倒；這裡逐章過濾，再把時間對齊到段落開頭，前端才能以段落定位。
"""
from __future__ import annotations

import bisect
import re

# 開頭補上的章節標題，用於模型給的第一章沒有從節目開頭開始時。
OPENING_TITLE = "開場"
# 少於此數的章節沒有導覽價值，整組不用。
MIN_CHAPTERS = 2
# 容許模型給的時間略超過節目長度（逐字稿時間戳只到秒）。
END_TOLERANCE_SEC = 5

_TIME = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2})$")


def parse_time(value: object) -> float | None:
    """把 hh:mm:ss 或 mm:ss 轉成秒數；格式不符時回傳 None。"""
    if not isinstance(value, str):
        return None
    match = _TIME.match(value.strip())
    if not match:
        return None
    hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
    # 沒寫小時的 mm:ss 容許分鐘超過 60（如 75:30），模型偶爾這樣寫。
    if seconds >= 60 or (match.group(1) and minutes >= 60):
        return None
    return hours * 3600 + minutes * 60 + seconds


def normalize(
    raw: object, segment_starts: list[float], *, duration: float | None = None
) -> list[dict]:
    """檢查模型回傳的章節，對齊到段落開頭。

    Args:
        raw: 模型回傳的 chapters，預期為 [{"start": "00:12:30", "title": "..."}]。
        segment_starts: 各段落的開始秒數，依時間排序。
        duration: 節目長度（秒）；未知時以最後一段的開始時間為準。

    Returns:
        [{"start": 秒數, "title": 標題}]，start 等於某一段的開始時間。
        不合格的章節逐一略過；剩不到 MIN_CHAPTERS 章時回傳空列表。
    """
    if not isinstance(raw, list) or not segment_starts:
        return []

    limit = max(duration or 0, segment_starts[-1]) + END_TOLERANCE_SEC
    # 逐字稿上的時間戳捨去了小數（pipeline._timestamp），模型照抄的時間
    # 會比段落實際開始早不到一秒，故以捨去後的秒數比對。
    floors = [int(start) for start in segment_starts]
    chapters: list[dict] = []
    last_index = -1
    for item in raw:
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        seconds = parse_time(item.get("start"))
        if not isinstance(title, str) or not title.strip() or seconds is None:
            continue
        if seconds > limit:
            continue

        # 不晚於該時間的最近一個段落；早於第一段時對齊到第一段。
        index = max(bisect.bisect_right(floors, seconds) - 1, 0)
        # 時間倒退，或與前一章對齊到同一段
        if index <= last_index:
            continue
        last_index = index
        chapters.append({"start": segment_starts[index], "title": title.strip()})

    if chapters and chapters[0]["start"] != segment_starts[0]:
        chapters.insert(0, {"start": segment_starts[0], "title": OPENING_TITLE})

    return chapters if len(chapters) >= MIN_CHAPTERS else []
