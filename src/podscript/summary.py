"""摘要、心智圖與標籤生成。

三項由同一次呼叫產生：逐字稿約 3 萬 token，分開呼叫會重複送入，
合併為一次是最省額度的做法。

以 SummaryProvider 抽象兩種後端：本機 claude CLI 與未來的 Anthropic API，
兩者失敗模式不同（額度用盡 vs HTTP 錯誤），各自處理。
"""
from __future__ import annotations

import json
import re
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

PROMPT = """請讀取 {path}，這是一集 Podcast 的逐字稿。

產生以下三項，並以 JSON 格式輸出：

1. summary：約 100 字的繁體中文摘要，須涵蓋整集重點，不可只寫開頭幾段的內容
2. mindmap：Mermaid mindmap 語法的架構心智圖，反映節目實際的討論脈絡
3. hashtags：5 個主題標籤，用於搜尋與分類

hashtags 規則：
- 不得使用人名（主持人、來賓、第三人皆不可）
- 以主題、領域、概念為準，例如 房地產、談判技巧、投資理財
- 不含 # 符號，每個標籤 2-6 字

輸出格式（只輸出 JSON，不要任何說明文字）：
{{"summary": "...", "mindmap": "mindmap\\n  root((主題))\\n    分支一\\n      細項", "hashtags": ["標籤一", "標籤二", "標籤三", "標籤四", "標籤五"]}}

注意：
- 逐字稿無標點符號，請依語意自行斷句理解
- SPEAKER_00 與 SPEAKER_01 是不同說話者
- mindmap 須為可直接渲染的合法 Mermaid 語法，階層以縮排表示"""

# 標籤收斂：生成階段不能讓標籤庫干擾 AI 選字，否則標籤會趨同、失去精準度，
# 故收斂為生成完成後的獨立第二次呼叫，只做同義判斷不重新生成。
RECONCILE_MODEL = "haiku"

RECONCILE_PROMPT = """現有標籤庫：
{existing}

本集新產生的標籤：
{new_tags}

請判斷每個「新標籤」是否與「現有標籤庫」中某個標籤語意相同或高度重疊
（例如「不動產」與「房地產」視為相同）。

輸出格式（只輸出 JSON，不要任何說明文字），鍵為新標籤、值為對應的現有標籤：
{{"新標籤一": "現有標籤X", "新標籤二": null}}

null 表示這是現有標籤庫中沒有涵蓋的新主題，應保留原新標籤。"""


@dataclass
class Summary:
    """摘要結果。"""

    summary: str
    mindmap: str
    hashtags: list[str]
    model: str
    usage: dict

    def to_dict(self) -> dict:
        return {
            "summary": self.summary,
            "mindmap": self.mindmap,
            "hashtags": self.hashtags,
            "model": self.model,
            "usage": self.usage,
        }


class SummaryError(Exception):
    """摘要生成失敗。"""


class SummaryProvider(ABC):
    """摘要與心智圖的生成後端。"""

    @abstractmethod
    def generate(self, transcript_path: Path, *, model: str) -> Summary:
        """讀取逐字稿，產生摘要與心智圖。

        Raises:
            SummaryError: 生成失敗或回傳格式無法解析。
        """


class ClaudeCliProvider(SummaryProvider):
    """透過本機 claude CLI 生成，消耗訂閱額度。"""

    def __init__(self, *, timeout: int = 600) -> None:
        self.timeout = timeout

    def generate(self, transcript_path: Path, *, model: str = "sonnet") -> Summary:
        if not transcript_path.exists():
            raise SummaryError(f"找不到逐字稿 {transcript_path}")

        # 逐字稿以檔案傳遞，不放進命令列參數：2-3 萬字會超過 ARG_MAX。
        try:
            proc = subprocess.run(
                [
                    "claude",
                    "-p",
                    PROMPT.format(path=transcript_path),
                    "--model",
                    model,
                    "--output-format",
                    "json",
                ],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired as exc:
            raise SummaryError(f"claude 生成逾時（{self.timeout}s）") from exc
        except FileNotFoundError as exc:
            raise SummaryError("找不到 claude CLI") from exc

        if proc.returncode != 0:
            raise SummaryError(f"claude 生成失敗：{proc.stderr.strip()[:300]}")

        return _parse_cli_output(proc.stdout, model=model)


def _parse_cli_output(stdout: str, *, model: str) -> Summary:
    """解析 claude CLI 的 JSON 輸出。

    CLI 以信封格式回傳，實際內容在 result 欄位；
    該欄位可能夾雜說明文字，故再從中擷取 JSON 區塊。
    """
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise SummaryError(f"CLI 輸出非 JSON：{stdout[:200]}") from exc

    content = envelope.get("result", "")
    payload = _extract_json(content)

    summary = payload.get("summary", "").strip()
    mindmap = payload.get("mindmap", "").strip()
    if not summary or not mindmap:
        raise SummaryError(f"回傳缺少 summary 或 mindmap：{content[:200]}")

    return Summary(
        summary=summary,
        mindmap=mindmap,
        hashtags=_clean_hashtags(payload.get("hashtags")),
        model=_resolve_model_id(envelope, fallback=model),
        usage=envelope.get("usage", {}),
    )


def _resolve_model_id(envelope: dict, *, fallback: str) -> str:
    """取出實際執行的模型 ID。

    CLI 的 modelUsage 以完整模型 ID 為鍵（如 claude-opus-5-20260401），
    比傳入的別名（opus）精確，便於日後比對不同版本的產出品質。
    多個模型參與時取用量最大者，即實際負責生成的模型。

    Args:
        fallback: 取不到時沿用的名稱，通常是呼叫端傳入的別名。
    """
    usage_by_model = envelope.get("modelUsage")
    if not isinstance(usage_by_model, dict) or not usage_by_model:
        return fallback

    def output_tokens(item: object) -> int:
        return item.get("outputTokens", 0) if isinstance(item, dict) else 0

    return max(usage_by_model, key=lambda k: output_tokens(usage_by_model[k]))


def _clean_hashtags(raw: object) -> list[str]:
    """整理標籤：去除 # 前綴與空白，最多保留 5 個。

    標籤僅供搜尋與分類，缺少時不影響摘要與心智圖，故不視為錯誤。
    """
    if not isinstance(raw, list):
        return []

    tags = []
    for item in raw:
        if not isinstance(item, str):
            continue
        tag = item.strip().lstrip("#").strip()
        if tag and tag not in tags:
            tags.append(tag)
    return tags[:5]


def _extract_json(text: str) -> dict:
    """從可能夾雜說明文字的回覆中取出 JSON 物件。"""
    text = text.strip()

    # 優先處理 ```json 圍欄
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 退而求其次：取第一個 { 到最後一個 } 之間的內容
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise SummaryError(f"回覆中找不到 JSON：{text[:200]}")

    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise SummaryError(f"JSON 解析失敗：{text[start:start + 200]}") from exc


def reconcile_hashtags(
    hashtags: list[str], existing: list[str], *, timeout: int = 120
) -> list[str]:
    """把新標籤與既有標籤庫比對，同義者收斂為既有標籤。

    另開一次獨立呼叫而非在生成 prompt 中帶入標籤庫，是為了不讓 AI
    生成階段趨向「挑選既有標籤」而犧牲對本集內容的精準命名。

    失敗（無標籤庫、CLI 錯誤、逾時、回傳格式異常）一律回傳原始 hashtags：
    標籤收斂是附加的品質改善，不可讓其失敗擋住摘要結果的可用性。
    """
    if not hashtags or not existing:
        return hashtags

    try:
        proc = subprocess.run(
            [
                "claude",
                "-p",
                RECONCILE_PROMPT.format(
                    existing=", ".join(existing),
                    new_tags=", ".join(hashtags),
                ),
                "--model",
                RECONCILE_MODEL,
                "--output-format",
                "json",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return hashtags

    if proc.returncode != 0:
        return hashtags

    try:
        envelope = json.loads(proc.stdout)
        mapping = _extract_json(envelope.get("result", ""))
    except (json.JSONDecodeError, SummaryError):
        return hashtags

    if not isinstance(mapping, dict):
        return hashtags

    resolved = [mapping.get(tag) or tag for tag in hashtags]
    deduped = []
    for tag in resolved:
        if isinstance(tag, str) and tag and tag not in deduped:
            deduped.append(tag)
    return deduped or hashtags


def get_provider(name: str = "claude_cli") -> SummaryProvider:
    """依設定取得對應的 provider。"""
    if name == "claude_cli":
        return ClaudeCliProvider()
    raise SummaryError(f"未知的 SUMMARY_PROVIDER：{name}")
