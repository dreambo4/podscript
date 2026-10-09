"""論文全文翻譯：英文論文逐段翻成繁體中文，原文與譯文可在介面切換。

與摘要分開呼叫（見 todo 第 8 項的決定）：合併只省一次固定開銷，輸出 token 不變，
且會變成每篇必翻、輸出易超上限、重新生成摘要會連帶重翻。

- 依段落順序分批（每批約 BATCH_CHARS 字元），同時送出 PARALLEL 批，縮短等待時間
- 每段譯文存在該段的 translation，標題譯文存在 episode.title_translated
- 參考文獻不翻譯（使用者決定）
- 每批完成就寫進 result 並呼叫 on_batch 存檔：服務重啟或部分批次失敗時，已翻好的不會遺失
- 已有譯文的段落預設略過：中途中斷或失敗時再按一次只補沒翻到的部分
- 譯文不經 OpenCC 轉換：模型輸出已是繁體，s2tw 會把兩岸通用字誤轉（佛羅里達→佛羅裡達、本台→本臺），
  2026-10-09 實測整篇譯文沒有簡體字，簡繁要求交給 prompt
"""
from __future__ import annotations

import json
import os
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

from . import summary
from .resolvers.paper import REFERENCE

# 每批原文字元數。英文約 1300 字，譯文約 3000 中文字，輸出遠低於 CLI 上限。
BATCH_CHARS = 8000
# 同時送出的批數。總額度不變，只是縮短等待時間。
PARALLEL = 3
# 標題在批次中的編號；段落編號從 0 起算，不會衝突。
TITLE_INDEX = -1
# 每批失敗時重試的次數（模型偶爾回傳無法解析的 JSON）。
RETRIES = 1

# (已完成批數, 總批數)
ProgressFn = Callable[[int, int], None]
# 每批譯文寫進 result 後呼叫，供呼叫端存檔；在同一個執行緒依序呼叫，不會同時執行。
BatchFn = Callable[[], None]


class TranslateError(Exception):
    """翻譯失敗。訊息需可直接顯示給使用者。"""


def default_model() -> str:
    return os.environ.get("TRANSLATE_MODEL", "sonnet")


def pending_count(result, *, force: bool = False) -> int:
    """還需要翻譯的段落數（不含參考文獻）。"""
    return len(_targets(result, force=force))


def translate(
    result,
    workdir: Path,
    *,
    force: bool = False,
    model: str | None = None,
    on_progress: ProgressFn | None = None,
    on_batch: BatchFn | None = None,
) -> int:
    """翻譯論文，譯文直接寫進 result 的各段與標題。

    Args:
        result: pipeline.Result，必須是論文。
        workdir: 存放每批暫存檔的目錄，claude CLI 從這裡讀取；用畢刪除。
        force: 已有譯文的段落也重翻。
        on_batch: 每批譯文寫進 result 後呼叫，供呼叫端存檔。

    Returns:
        這次補上譯文的段落數。

    Raises:
        TranslateError: 全部批次都失敗；部分失敗時已翻好的仍會寫入，並以此回報。
    """
    model = model or default_model()
    notify = on_progress or (lambda done, total: None)
    targets = _targets(result, force=force)
    include_title = force or not result.episode.title_translated
    batches = _batches(result, targets, include_title=include_title)
    if not batches:
        return 0

    provider = summary.get_provider(os.environ.get("SUMMARY_PROVIDER", "claude_cli"))
    workdir.mkdir(parents=True, exist_ok=True)
    count = 0
    errors: list[str] = []
    done = 0
    notify(done, len(batches))
    try:
        with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
            futures = [
                pool.submit(_translate_batch, provider, workdir / f"batch-{n}.json", batch, model)
                for n, batch in enumerate(batches)
            ]
            for future in as_completed(futures):
                try:
                    count += _apply(result, future.result(), targets)
                    if on_batch:
                        on_batch()
                except summary.SummaryError as exc:
                    errors.append(str(exc))
                done += 1
                notify(done, len(batches))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    if errors and not count:
        raise TranslateError(f"翻譯失敗：{errors[0]}")
    if errors:
        raise TranslateError(
            f"有 {len(errors)} 批翻譯失敗（已完成的譯文已儲存），再按一次「翻譯」補上剩下的部分"
        )
    return count


def _apply(result, translated: dict[int, str], targets: set[int]) -> int:
    """把一批譯文寫進 result，回傳寫入的段落數（不含標題）。"""
    count = 0
    for index, text in translated.items():
        if index == TITLE_INDEX:
            result.episode.title_translated = text
        elif 0 <= index < len(result.segments) and index in targets:
            result.segments[index].translation = text
            count += 1
    return count


def _targets(result, *, force: bool) -> set[int]:
    return {
        i
        for i, seg in enumerate(result.segments)
        if seg.kind != REFERENCE and seg.text.strip() and (force or not seg.translation)
    }


def _batches(result, targets: set[int], *, include_title: bool) -> list[list[dict]]:
    """依段落順序分批，不拆開單一段落；超過上限的單段（大表格）自成一批。"""
    items = []
    if include_title:
        items.append({"i": TITLE_INDEX, "kind": "title", "text": result.episode.title})
    items += [
        {"i": i, "kind": seg.kind or "p", "text": seg.text}
        for i, seg in enumerate(result.segments)
        if i in targets
    ]

    batches: list[list[dict]] = []
    current: list[dict] = []
    size = 0
    for item in items:
        length = len(item["text"])
        if current and size + length > BATCH_CHARS:
            batches.append(current)
            current, size = [], 0
        current.append(item)
        size += length
    if current:
        batches.append(current)
    return batches


def _translate_batch(provider, path: Path, batch: list[dict], model: str) -> dict[int, str]:
    path.write_text(json.dumps(batch, ensure_ascii=False, indent=1), encoding="utf-8")
    wanted = {item["i"] for item in batch}
    last_error: summary.SummaryError | None = None
    for _ in range(RETRIES + 1):
        try:
            result = provider.generate_translation(path, model=model)
        except summary.SummaryError as exc:
            last_error = exc
            continue
        # 模型自創的編號不收
        return {i: text for i, text in result.items() if i in wanted}
    raise last_error or summary.SummaryError("翻譯失敗")
