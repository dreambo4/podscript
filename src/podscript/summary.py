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
from dataclasses import dataclass, field
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

請判斷每個「新標籤」是否與「現有標籤庫」中某個標籤為同義詞，
即換成該現有標籤後意思不變。以下都算同義，應對應到現有標籤：
- 同義用詞（例如「不動產」對應「房地產」）
- 縮寫或外文名稱（例如「ADHD」對應「注意力不足」）
- 語序或字詞微調（例如「理財投資」對應「投資理財」）

以下不算同義，輸出 null：
- 範圍較小或較大的相關概念（例如「買房殺價」不等於「房地產」）
- 同一領域但不同面向（例如「房貸」不等於「投資理財」）
- 字面相近但指涉不同（例如「職場心理」不等於「心理健康」）

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
    # 標籤收斂的合併建議，每項為 {"from", "to", "keep"}；見 find_hashtag_merges。
    hashtag_merges: list[dict] = field(default_factory=list)
    # 合併前的原始標籤，供使用者改變決定時重新計算 hashtags。
    hashtags_generated: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "summary": self.summary,
            "mindmap": self.mindmap,
            "hashtags": self.hashtags,
            "hashtag_merges": self.hashtag_merges,
            "hashtags_generated": self.hashtags_generated,
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


def find_hashtag_merges(
    hashtags: list[str], existing: list[str], *, timeout: int = 120
) -> list[dict]:
    """把新標籤與既有標籤庫比對，找出可收斂為既有標籤的同義者。

    另開一次獨立呼叫而非在生成 prompt 中帶入標籤庫，是為了不讓 AI
    生成階段趨向「挑選既有標籤」而犧牲對本集內容的精準命名。

    只回傳合併建議，不直接替換：AI 的同義判斷不穩定且常過度合併，
    是否合併由使用者逐項確認（見 decide_hashtag_merges）。

    失敗（無標籤庫、CLI 錯誤、逾時、回傳格式異常）一律回傳空列表：
    標籤收斂是附加的品質改善，不可讓其失敗擋住摘要結果的可用性。

    Returns:
        每項為 {"from": 新標籤, "to": 既有標籤, "keep": None}，
        keep 待使用者決定。
    """
    if not hashtags or not existing:
        return []

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
        return []

    if proc.returncode != 0:
        return []

    try:
        envelope = json.loads(proc.stdout)
        mapping = _extract_json(envelope.get("result", ""))
    except (json.JSONDecodeError, SummaryError):
        return []

    if not isinstance(mapping, dict):
        return []

    # 只接受標籤庫中確實存在的對應，避免 AI 自創標籤混入。
    return [
        {"from": tag, "to": mapping[tag], "keep": None}
        for tag in hashtags
        if isinstance(mapping.get(tag), str)
        and mapping[tag] in existing
        and mapping[tag] != tag
    ]


def pending_hashtag_merges(summary: dict) -> list[dict]:
    """尚未決定的合併建議。有任何一項未決定前不可上傳。"""
    return [m for m in summary.get("hashtag_merges") or [] if m.get("keep") is None]


def decide_hashtag_merges(summary: dict, decisions: dict[str, str]) -> dict:
    """套用使用者對合併建議的決定，重新計算標籤。

    Args:
        summary: result.json 的內容，會直接修改。
        decisions: {新標籤: 要保留的標籤}，值須為該項的 from 或 to。
            未列出的項目維持原狀，可分次決定、也可改變先前的決定。

    Raises:
        ValueError: 新標籤不在合併建議中，或保留的標籤不是 from/to 之一。
    """
    merges = summary.get("hashtag_merges") or []
    by_from = {m["from"]: m for m in merges}
    for tag, keep in decisions.items():
        merge = by_from.get(tag)
        if merge is None:
            raise ValueError(f"「{tag}」沒有待確認的合併")
        if keep not in (merge["from"], merge["to"]):
            raise ValueError(f"「{tag}」只能保留「{merge['from']}」或「{merge['to']}」")
        merge["keep"] = keep

    # 從合併前的原始標籤重算，保持生成時的順序；
    # 決定合併者換成既有標籤，多個標籤合併為同一個時只留一個。
    chosen = {m["from"]: m["keep"] for m in merges if m["keep"] is not None}
    tags = []
    for tag in summary.get("hashtags_generated") or summary.get("hashtags", []):
        final = chosen.get(tag, tag)
        if final not in tags:
            tags.append(final)
    summary["hashtags"] = tags
    return summary


def get_provider(name: str = "claude_cli") -> SummaryProvider:
    """依設定取得對應的 provider。"""
    if name == "claude_cli":
        return ClaudeCliProvider()
    raise SummaryError(f"未知的 SUMMARY_PROVIDER：{name}")
