"""摘要、心智圖、標籤、章節與封面生成。

五項由同一次呼叫產生：逐字稿約 3 萬 token，分開呼叫會重複送入，
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

from . import cover as cover_module
from .cover import COVER_EXAMPLE, COVER_RULES

# 業配章節規則，Podcast 與文章共用。
SPONSOR_RULES = """- 內容中若有業配、廣告、贊助商介紹、自身的商品推銷等段落，即使很短也要獨立成一章，
  title 以「業配：」開頭並寫出品牌或商品，例如「業配：Hostinger 架站工具」
- 沒有這類段落就不要產生業配章節；只是提到品牌或產品的一般討論不算業配，不可用「業配：」開頭"""

# 章節規則，摘要 prompt 與只補章節的 prompt 共用。
CHAPTER_RULES = """chapters 規則：
- 依話題轉換切分章節，約每 8 到 12 分鐘一章，至少 3 章、最多 15 章
- start 為該章第一段的時間戳，照抄逐字稿上 [ ] 內的時間，不可自行推算
- 第一章從逐字稿開頭開始，之後依時間先後排列
- title 為 6 到 16 字的繁體中文，寫出該章實際討論的內容，不用「第一章」「開場白」這類空泛名稱
""" + SPONSOR_RULES

# 文章的段落沒有時間，以段落編號定位。
ARTICLE_CHAPTER_RULES = """chapters 規則：
- 依論述主題轉換切分章節，約每 1000 字一章，至少 2 章、最多 12 章
- paragraph 為該章第一段的編號，照抄全文中每段開頭 [ ] 內的數字（整數）
- 第一章從第 1 段開始，之後依段落先後排列
- title 為 6 到 16 字的繁體中文，寫出該章實際的內容，不用「第一章」「前言」這類空泛名稱
""" + SPONSOR_RULES

PROMPT = """請讀取 {path}，這是一集 Podcast 的逐字稿。

產生以下五項，並以 JSON 格式輸出：

1. summary：約 100 字的繁體中文摘要，須涵蓋整集重點，不可只寫開頭幾段的內容
2. mindmap：Mermaid mindmap 語法的架構心智圖，反映節目實際的討論脈絡
3. hashtags：最多 5 個主題標籤，用於搜尋與分類
4. chapters：章節目錄，供使用者跳到想聽的段落
5. cover：代表本集內容的封面插圖，顯示在列表卡片上

hashtags 規則：
- 不得使用人名（主持人、來賓、第三人皆不可）
- 以主題、領域、概念為準，例如 房地產、談判技巧、投資理財
- 不含 # 符號，每個標籤 2-6 字
- 標籤之間語意不可重疊，例如「投資理財」與「理財規劃」只能留一個
- 主題不夠多元時寧可少於 5 個，不要用近義詞湊數

""" + CHAPTER_RULES + """

""" + COVER_RULES + """

輸出格式（只輸出 JSON，不要任何說明文字）：
{{"summary": "...", "mindmap": "mindmap\\n  root((主題))\\n    分支一\\n      細項", "hashtags": ["標籤一", "標籤二", "標籤三"], "chapters": [{{"start": "00:00", "title": "..."}}, {{"start": "12:30", "title": "..."}}], """ + COVER_EXAMPLE + """}}

注意：
- 逐字稿無標點符號，請依語意自行斷句理解
- SPEAKER_00 與 SPEAKER_01 是不同說話者
- mindmap 須為可直接渲染的合法 Mermaid 語法，階層以縮排表示"""

ARTICLE_PROMPT = """請讀取 {path}，這是一篇文章（新聞、評論或專欄等）的全文。

產生以下五項，並以 JSON 格式輸出：

1. summary：約 100 字的繁體中文摘要，須涵蓋全文重點，不可只寫導言或開頭幾段的內容
2. mindmap：Mermaid mindmap 語法的架構心智圖，反映文章實際的論述脈絡
3. hashtags：最多 5 個主題標籤，用於搜尋與分類
4. chapters：章節目錄，供使用者跳到想讀的段落
5. cover：代表本文內容的封面插圖，顯示在列表卡片上

hashtags 規則：
- 不得使用人名（作者、受訪者、文中提及的人物皆不可）
- 以主題、領域、概念為準，例如 房地產、談判技巧、投資理財
- 不含 # 符號，每個標籤 2-6 字
- 標籤之間語意不可重疊，例如「投資理財」與「理財規劃」只能留一個
- 主題不夠多元時寧可少於 5 個，不要用近義詞湊數

""" + ARTICLE_CHAPTER_RULES + """

""" + COVER_RULES + """

輸出格式（只輸出 JSON，不要任何說明文字）：
{{"summary": "...", "mindmap": "mindmap\\n  root((主題))\\n    分支一\\n      細項", "hashtags": ["標籤一", "標籤二", "標籤三"], "chapters": [{{"paragraph": 1, "title": "..."}}, {{"paragraph": 8, "title": "..."}}], """ + COVER_EXAMPLE + """}}

注意：
- 全文由網頁自動擷取，結尾可能夾雜相關新聞標題、發布時間列表、「繼續閱讀」等網站雜訊，請忽略
- 原文以外的資訊不要寫進摘要與心智圖
- mindmap 須為可直接渲染的合法 Mermaid 語法，階層以縮排表示"""

# 論文的章節直接用原有的段落標題（見 pipeline），不由模型分章；
# 改為順便讀出期刊名稱等書目資料，列表上的來源欄位才有意義。
PAPER_PROMPT = """請讀取 {path}，這是一篇學術論文的全文（由 PDF 擷取，參考文獻已省略）。

產生以下五項，並以 JSON 格式輸出：

1. summary：約 400 字的繁體中文摘要（可依論文份量增減），依序說明研究問題、研究方法、主要發現、研究限制，
   須涵蓋全文重點，不可只改寫論文的 Abstract；論文未提及限制時寫「作者未明確說明」
2. mindmap：Mermaid mindmap 語法的架構心智圖，根節點為論文主題，
   第一層依序為「研究問題」「研究方法」「主要發現」「研究限制」，其下再列細項
3. hashtags：最多 5 個主題標籤，用於搜尋與分類
4. meta：論文的書目資料，照抄原文，不翻譯
5. cover：代表本文內容的封面插圖，顯示在列表卡片上

hashtags 規則：
- 不得使用人名（作者、文中提及的研究者皆不可）
- 以主題、領域、概念為準，例如 運動生理、減重、代謝
- 不含 # 符號，每個標籤 2-6 字，使用繁體中文
- 標籤之間語意不可重疊，例如「投資理財」與「理財規劃」只能留一個
- 主題不夠多元時寧可少於 5 個，不要用近義詞湊數

meta 規則：
- title：論文標題，照抄原文
- journal：期刊或會議名稱，照抄原文；找不到時為空字串
- first_author：第一作者的姓（family name）；找不到時為空字串
- published：發表日期，格式 YYYY-MM-DD；只知道年份時為 YYYY；找不到時為空字串

""" + COVER_RULES + """

輸出格式（只輸出 JSON，不要任何說明文字）：
{{"summary": "...", "mindmap": "mindmap\\n  root((主題))\\n    研究問題\\n      細項", "hashtags": ["標籤一", "標籤二"], "meta": {{"title": "...", "journal": "...", "first_author": "...", "published": "2009-05-14"}}, """ + COVER_EXAMPLE + """}}

注意：
- 全文由 PDF 自動擷取，可能夾雜頁首頁尾、圖內文字與亂碼的數學式，請忽略
- 論文以外的資訊不要寫進摘要與心智圖
- 專有名詞第一次出現時可附原文，例如「靜態代謝率（REE）」
- mindmap 須為可直接渲染的合法 Mermaid 語法，階層以縮排表示"""

# 已有摘要的集數只補章節時使用，不重產摘要、心智圖與標籤。
CHAPTERS_PROMPT = """請讀取 {path}，這是一集 Podcast 的逐字稿。

產生章節目錄，供使用者跳到想聽的段落，並以 JSON 格式輸出。

""" + CHAPTER_RULES + """

輸出格式（只輸出 JSON，不要任何說明文字）：
{{"chapters": [{{"start": "00:00", "title": "..."}}, {{"start": "12:30", "title": "..."}}]}}

注意：
- 逐字稿無標點符號，請依語意自行斷句理解
- SPEAKER_00 與 SPEAKER_01 是不同說話者"""

ARTICLE_CHAPTERS_PROMPT = """請讀取 {path}，這是一篇文章（新聞、評論或專欄等）的全文。

產生章節目錄，供使用者跳到想讀的段落，並以 JSON 格式輸出。

""" + ARTICLE_CHAPTER_RULES + """

輸出格式（只輸出 JSON，不要任何說明文字）：
{{"chapters": [{{"paragraph": 1, "title": "..."}}, {{"paragraph": 8, "title": "..."}}]}}

注意：
- 全文由網頁自動擷取，結尾可能夾雜相關新聞標題、「繼續閱讀」等網站雜訊，不要為雜訊另立章節"""

# 只重畫封面時使用：依標題、摘要與標籤畫，不讀逐字稿，省去整份逐字稿的額度。
COVER_PROMPT = """以下是一集節目（或一篇文章）的資訊：

標題：{title}
摘要：{summary}
標籤：{hashtags}

依內容畫一個封面插圖，並以 JSON 格式輸出。

""" + COVER_RULES + """

輸出格式（只輸出 JSON，不要任何說明文字）：
{{""" + COVER_EXAMPLE + """}}"""

# 論文翻譯：與摘要分開呼叫，按段落分批，見 translate 模組。
# 譯文不放在命令列參數：每批數千字，一律寫檔讓 claude 讀。
TRANSLATE_PROMPT = """請讀取 {path}，這是一篇英文學術論文的其中一部分，已切成段落，以 JSON 陣列存放。
每個元素有 i（編號）、kind（類型）、text（原文）。

請把每一段翻成繁體中文（台灣用語），以 JSON 格式輸出。

翻譯規則：
- 忠實翻譯，不摘要、不省略、不加入原文沒有的說明
- 學術書面語氣；專有名詞與縮寫（如 VLCHP、DXA、HOMA-IR）保留原文，
  常見術語可譯成中文後附原文，例如「靜態代謝率（REE）」，同一個詞只在第一次出現時附原文
- 數字、單位、統計量（P < 0.05、95% CI）、引用編號（如 [12]、[3-7]）照抄不改
- kind 為 title、h1、h2 的是標題，譯成簡短的標題，不加句號
- kind 為 table 的是表格，每一行是一列：行數必須與原文相同，只翻譯文字（表格標題、欄名、列名），
  數字與符號照抄，行與行之間以 \\n 分隔
- 原文中的頁首頁尾、圖內文字或亂碼的數學式，照抄原文即可，不要硬譯

輸出格式（只輸出 JSON，不要任何說明文字）：
{{"items": [{{"i": 0, "text": "譯文"}}, {{"i": 1, "text": "譯文"}}]}}

每個輸入段落都要有對應的譯文，i 照抄輸入的編號。"""

# 依內容類型選用的提示；鍵對應 pipeline 的 content_kind。
PROMPTS = {"podcast": PROMPT, "article": ARTICLE_PROMPT, "paper": PAPER_PROMPT}
CHAPTERS_PROMPTS = {"podcast": CHAPTERS_PROMPT, "article": ARTICLE_CHAPTERS_PROMPT}

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
    # 模型回傳的原始章節；由 pipeline 依段落時間檢查與對齊後覆寫，見 chapters.normalize。
    chapters: list = field(default_factory=list)
    # 封面 {"svg", "color"}，已過濾；模型沒畫或畫壞時為 None，見 cover.normalize。
    cover: dict | None = None
    # 論文的書目資料 {"title", "journal", "first_author", "published"}；其他內容為 None。
    meta: dict | None = None

    def to_dict(self) -> dict:
        return {
            "summary": self.summary,
            "mindmap": self.mindmap,
            "hashtags": self.hashtags,
            "chapters": self.chapters,
            "cover": self.cover,
            "meta": self.meta,
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
    def generate(
        self, transcript_path: Path, *, model: str, kind: str = "podcast"
    ) -> Summary:
        """讀取逐字稿或文章全文，產生摘要與心智圖。

        Args:
            kind: 內容類型，podcast、article 或 paper，決定使用的提示。

        Raises:
            SummaryError: 生成失敗或回傳格式無法解析。
        """

    @abstractmethod
    def generate_chapters(
        self, transcript_path: Path, *, model: str, kind: str = "podcast"
    ) -> list:
        """只產生章節，供已有摘要的集數補上。

        Args:
            kind: 內容類型，podcast 或 article，決定使用的提示。

        Returns:
            模型回傳的原始章節，尚未檢查與對齊。

        Raises:
            SummaryError: 生成失敗或回傳格式無法解析。
        """

    @abstractmethod
    def generate_cover(
        self, *, title: str, summary: str, hashtags: list[str], model: str
    ) -> dict:
        """只重畫封面，依標題、摘要與標籤，不讀逐字稿。

        Returns:
            過濾後的封面 {"svg", "color"}，見 cover.normalize。

        Raises:
            SummaryError: 生成失敗，或模型回傳的封面不可用。
        """


    @abstractmethod
    def generate_translation(self, chunk_path: Path, *, model: str) -> dict[int, str]:
        """翻譯一批論文段落。

        Args:
            chunk_path: JSON 陣列檔，每個元素為 {"i", "kind", "text"}。

        Returns:
            {編號: 譯文}；模型漏掉的段落不在其中。

        Raises:
            SummaryError: 生成失敗或回傳格式無法解析。
        """


class ClaudeCliProvider(SummaryProvider):
    """透過本機 claude CLI 生成，消耗訂閱額度。"""

    def __init__(self, *, timeout: int = 600) -> None:
        self.timeout = timeout

    def generate(
        self, transcript_path: Path, *, model: str = "sonnet", kind: str = "podcast"
    ) -> Summary:
        if not transcript_path.exists():
            raise SummaryError(f"找不到逐字稿 {transcript_path}")
        stdout = self._run(PROMPTS[kind].format(path=transcript_path), model=model)
        return _parse_cli_output(stdout, model=model)

    def generate_chapters(
        self, transcript_path: Path, *, model: str = "sonnet", kind: str = "podcast"
    ) -> list:
        if not transcript_path.exists():
            raise SummaryError(f"找不到逐字稿 {transcript_path}")
        stdout = self._run(CHAPTERS_PROMPTS[kind].format(path=transcript_path), model=model)
        try:
            envelope = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise SummaryError(f"CLI 輸出非 JSON：{stdout[:200]}") from exc
        chapters = _extract_json(envelope.get("result", "")).get("chapters")
        if not isinstance(chapters, list):
            raise SummaryError("回傳缺少 chapters")
        return chapters

    def generate_cover(
        self, *, title: str, summary: str, hashtags: list[str], model: str = "sonnet"
    ) -> dict:
        prompt = COVER_PROMPT.format(
            title=title, summary=summary, hashtags="、".join(hashtags) or "（無）"
        )
        stdout = self._run(prompt, model=model)
        try:
            envelope = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise SummaryError(f"CLI 輸出非 JSON：{stdout[:200]}") from exc
        cover = cover_module.normalize(_extract_json(envelope.get("result", "")).get("cover"))
        if cover is None:
            raise SummaryError("模型回傳的封面格式不正確，請再試一次")
        return cover

    def generate_translation(self, chunk_path: Path, *, model: str = "sonnet") -> dict[int, str]:
        stdout = self._run(TRANSLATE_PROMPT.format(path=chunk_path), model=model)
        try:
            envelope = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise SummaryError(f"CLI 輸出非 JSON：{stdout[:200]}") from exc
        items = _extract_json(envelope.get("result", "")).get("items")
        if not isinstance(items, list):
            raise SummaryError("回傳缺少 items")
        return {
            item["i"]: item["text"].strip()
            for item in items
            if isinstance(item, dict)
            and isinstance(item.get("i"), int)
            and isinstance(item.get("text"), str)
            and item["text"].strip()
        }

    def _run(self, prompt: str, *, model: str) -> str:
        """執行 claude CLI，回傳原始輸出。"""
        # 逐字稿以檔案傳遞，不放進命令列參數：2-3 萬字會超過 ARG_MAX。
        try:
            proc = subprocess.run(
                [
                    "claude",
                    "-p",
                    prompt,
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
        return proc.stdout


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
        # 章節有問題不影響摘要，格式檢查留給 chapters.normalize 逐章過濾。
        chapters=payload.get("chapters") if isinstance(payload.get("chapters"), list) else [],
        # 封面同樣不影響摘要，畫壞時為 None。
        cover=cover_module.normalize(payload.get("cover")),
        meta=_clean_meta(payload.get("meta")),
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


def _clean_meta(raw: object) -> dict | None:
    """整理論文書目資料：只留字串欄位，去除空白。缺少時為 None，不影響摘要。"""
    if not isinstance(raw, dict):
        return None
    meta = {
        key: raw[key].strip()
        for key in ("title", "journal", "first_author", "published")
        if isinstance(raw.get(key), str) and raw[key].strip()
    }
    return meta or None


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
