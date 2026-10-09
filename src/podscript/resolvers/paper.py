"""論文 PDF 的全文擷取：分段落、辨識標題、表格與參考文獻。

PDF 只有一行一行的文字與座標，沒有段落結構。這裡以 pypdfium2 逐字取出
字級、粗細與位置，組成行後再判斷：
- 標題：比內文大或粗體的短行
- 表格：欄與欄之間有大空隙的行，每列保留為一行，不併成段落
- 段落：同欄連續的內文行，依縮排、行距與句尾判斷分段
- 頁首頁尾：多頁重複出現在頁面上下緣的行，整行移除
- 圖內文字：遠小於內文的零碎短行，移除

雙欄的閱讀順序沿用 pdfium 的文字順序（實測能正確分欄）；
pdfium 以 U+FFFE 標記行尾斷字，可準確接回。
"""
from __future__ import annotations

import ctypes
import re
import statistics
from collections import Counter
from dataclasses import dataclass, field

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from ..storage import paper_guid
from .base import Episode, ResolveError

PLATFORM = "paper"

# 抽出的文字少於此字數視為掃描檔或加密檔。
MIN_CHARS = 500

# 段落片段的類型，存在 transcript 片段的 kind 欄位；一般段落為空字串。
HEADING = "h1"
SUBHEADING = "h2"
TABLE = "table"
REFERENCE = "ref"

# pdfium 標記行尾斷字的字元。
SOFT_HYPHEN = "￾"

REFERENCES_TITLE = re.compile(
    r"^(\d+\.?\s*)?(references?|bibliography|literature cited|works cited|參考文獻|参考文献|引用文獻)$",
    re.I,
)
ABSTRACT_TITLE = re.compile(r"^(abstract|summary|摘要|中文摘要)$", re.I)
FIGURE_CAPTION = re.compile(r"^(fig\.?|figure|圖)\s*[0-9]", re.I)
# 表格標題：「Table 1.」「TABLE II」「表 3：」；內文的「Table 3 shows」不算
TABLE_CAPTION = re.compile(
    r"^(table|tab\.)\s*[0-9IVX]+\s*([.:|]|$)|^表\s*[0-9一二三四五六七八九十]+\s*([.:：、]|\s|$)", re.I
)
SUBSECTION_NUMBER = re.compile(r"^\d+\.\d+")
# 參考文獻每筆的開頭：[12]、12.、12 後接大寫字母
REFERENCE_START = re.compile(r"^(\[\d+\]|\d{1,3}\.\s|\d{1,3}\s+[A-Z])")
SENTENCE_END = re.compile(r"[.!?。！？：:」』]$")
# 只有數字與運算符號的行：座標軸刻度
NUMBERS_ONLY = re.compile(r"^[\d\s.,%()+\-−–×=<>/]+$")
# 控制字元：數學字型對應錯誤時會出現
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f]")
PRIVATE_USE = re.compile(r"[\ue000-\uf8ff]")
CJK = re.compile(r"[　-〿㐀-鿿＀-￯]")


@dataclass
class Block:
    """論文的一個段落片段。

    Attributes:
        kind: 類型，見 HEADING 等常數；一般段落為空字串。
        text: 內容；表格每列以換行分隔。
    """

    kind: str
    text: str


@dataclass
class Paper:
    """論文解析結果。

    Attributes:
        episode: 對應到單集欄位的中繼資料。podcast_name 先填「論文」，
            摘要時由模型讀出期刊名稱後更新。
        blocks: 依閱讀順序排列的段落片段。
        pdf: PDF 原檔內容，上傳時存到 Storage。
    """

    episode: Episode
    blocks: list[Block]
    pdf: bytes = field(repr=False, default=b"")


@dataclass
class _Line:
    text: str
    size: float  # 字級（寬鬆字框高度的中位數）
    bold: bool
    italic: bool
    x0: float
    x1: float
    top: float  # 第一個字的上緣，由頁面上緣往下算，越大越下面
    bottom: float  # 最後一個字的下緣
    last_x1: float  # 最後一個視覺行的右緣，判斷段落是否在此結束
    page: int
    page_height: float
    gaps: int  # 欄距等級的大空隙數，表格列會有多個


def from_pdf(pdf: bytes, *, filename: str = "") -> Paper:
    """擷取 PDF 全文。

    Args:
        filename: 原始檔名，抓不到標題時備用。

    Raises:
        ResolveError: 不是 PDF、檔案損毀，或抽不到文字（掃描檔）。
    """
    if not pdf.startswith(b"%PDF-"):
        raise ResolveError("不是 PDF 檔")
    try:
        document = pdfium.PdfDocument(pdf)
    except pdfium.PdfiumError as exc:
        raise ResolveError(f"PDF 無法開啟（可能已損毀或有密碼保護）：{exc}") from exc

    try:
        lines = _read_lines(document)
    finally:
        document.close()

    if sum(len(line.text) for line in lines) < MIN_CHARS:
        raise ResolveError("這份 PDF 抽不到文字，可能是掃描檔；目前不支援掃描版 PDF")

    lines = _drop_page_furniture(lines)
    body_size = _body_size(lines)
    title, title_lines = _guess_title(lines, body_size)
    blocks = _build_blocks(
        [line for i, line in enumerate(lines) if i not in title_lines], body_size
    )
    title = title or _title_from_filename(filename)

    episode = Episode(
        platform=PLATFORM,
        source_url="",
        episode_guid=paper_guid(pdf),
        podcast_name="論文",
        title=title or "未命名論文",
        mp3_url="",
    )
    return Paper(episode=episode, blocks=blocks, pdf=pdf)


# ── 讀取行 ────────────────────────────────────────


def _read_lines(document: pdfium.PdfDocument) -> list[_Line]:
    lines: list[_Line] = []
    for index in range(len(document)):
        page = document[index]
        textpage = page.get_textpage()
        height = page.get_height()
        chars: list[tuple[str, tuple, bool]] = []
        for i in range(textpage.count_chars()):
            ch = chr(pdfium_c.FPDFText_GetUnicode(textpage, i))
            if ch == "\n":
                _append_line(lines, chars, index, height)
                chars = []
                continue
            if ch == "\r":
                continue
            if ch.strip() and ch != SOFT_HYPHEN:
                box = textpage.get_charbox(i, loose=True)
                chars.append((ch, box, _font_style(textpage, i)))
            else:
                chars.append((ch, None, (False, False)))
        _append_line(lines, chars, index, height)
        textpage.close()
        page.close()
    return lines


def _font_style(textpage, index: int) -> tuple[bool, bool]:
    """(粗體, 斜體)。字重常缺漏，另以字型名稱判斷。"""
    buffer = ctypes.create_string_buffer(128)
    flags = ctypes.c_int()
    pdfium_c.FPDFText_GetFontInfo(textpage, index, buffer, 128, ctypes.byref(flags))
    name = buffer.value.decode(errors="ignore").lower()
    bold = pdfium_c.FPDFText_GetFontWeight(textpage, index) >= 600 or any(
        mark in name for mark in ("bold", "medi", "black", "heavy", "semibold")
    )
    italic = any(mark in name for mark in ("ital", "oblique"))
    return bold, italic


def _append_line(lines: list[_Line], chars: list, page: int, height: float) -> None:
    visible = [c for c in chars if c[1] is not None]
    if not visible:
        return
    boxes = [c[1] for c in visible]
    size = statistics.median(b[3] - b[1] for b in boxes)
    # 旋轉的文字（如 arXiv 左側的編號）字框又高又窄，不是內文
    if size <= 0 or size > 3 * max(b[2] - b[0] for b in boxes) and len(visible) > 5:
        return
    gaps = sum(
        1
        for a, b in zip(boxes, boxes[1:])
        if b[0] - a[2] > size  # 超過一個字寬的空隙：欄距，而非字距
    )
    text = "".join(c[0] for c in chars).strip()
    # 行尾斷字時 pdfium 不換行，一行可能跨兩個視覺行；段落判斷要看最後一個視覺行
    lowest = min(b[1] for b in boxes)
    last_row = [b for b in boxes if b[1] < lowest + size * 0.5]
    lines.append(
        _Line(
            text=text,
            size=size,
            bold=sum(1 for c in visible if c[2][0]) / len(visible) >= 0.6,
            italic=sum(1 for c in visible if c[2][1]) / len(visible) >= 0.6,
            x0=min(b[0] for b in boxes),
            x1=max(b[2] for b in boxes),
            top=height - max(b[3] for b in boxes),
            bottom=height - lowest,
            last_x1=max(b[2] for b in last_row),
            page=page,
            page_height=height,
            gaps=gaps,
        )
    )


# ── 頁首頁尾 ──────────────────────────────────────

# 頁面上下緣的比例，頁首頁尾只會出現在這個範圍
MARGIN_RATIO = 0.1
PAGE_NUMBER = re.compile(r"^(page\s*)?\d+(\s*(of|/)\s*\d+)?$|^-\s*\d+\s*-$", re.I)


def _drop_page_furniture(lines: list[_Line]) -> list[_Line]:
    """移除頁首頁尾：多頁重複的行（數字視為相同）與單獨的頁碼。"""
    pages = len({line.page for line in lines})

    def in_margin(line: _Line) -> bool:
        margin = line.page_height * MARGIN_RATIO
        return line.top < margin or line.top > line.page_height - margin

    def key(line: _Line) -> str:
        return re.sub(r"\d+", "#", line.text.lower())

    counts = Counter(key(line) for line in lines if in_margin(line))
    threshold = 3 if pages >= 4 else 2
    return [
        line
        for line in lines
        if not (
            in_margin(line)
            and (PAGE_NUMBER.match(line.text) or (pages > 1 and counts[key(line)] >= threshold))
        )
    ]


# ── 標題與內文字級 ────────────────────────────────


def _body_size(lines: list[_Line]) -> float:
    """內文字級：以字數加權的最常見字級。"""
    weights: Counter[float] = Counter()
    for line in lines:
        weights[_size_key(line)] += len(line.text)
    return weights.most_common(1)[0][0] if weights else 10.0


def _size_key(line: _Line) -> float:
    return round(line.size * 2) / 2


def _guess_title(lines: list[_Line], body_size: float) -> tuple[str, set[int]]:
    """論文標題：第一頁上半部字級最大、字型相同的連續行。

    Returns:
        (標題, 標題各行在 lines 中的索引)；找不到時為 ("", 空集合)。
    """
    first = [
        i
        for i, line in enumerate(lines)
        if line.page == 0 and line.top < line.page_height * 0.6 and len(line.text) > 3
    ]
    # 依字級由大到小找；期刊名、刊頭字雖大但太短，跳過
    for size in sorted({_size_key(lines[i]) for i in first}, reverse=True):
        if size < body_size * 1.15:
            break
        picked: list[int] = []
        for i in first:
            line = lines[i]
            if not picked:
                if _size_key(line) == size:
                    picked.append(i)
                continue
            prev = lines[picked[-1]]
            # 同字級、同粗細、緊接在下一行；作者名單常與標題同字級但不同字型
            if (
                abs(line.size - prev.size) < 0.3
                and line.bold == prev.bold
                and 0 <= line.top - prev.bottom < prev.size
            ):
                picked.append(i)
            else:
                break
        title = _join([lines[i].text for i in picked])
        if len(title.split()) >= 4 or (CJK.search(title) and len(title) >= 6):
            return title, set(picked)
    return "", set()


def _title_from_filename(filename: str) -> str:
    stem = re.sub(r"\.pdf$", "", filename.strip(), flags=re.I)
    return stem.replace("_", " ").strip()


# ── 組成段落 ──────────────────────────────────────


def _build_blocks(lines: list[_Line], body_size: float) -> list[Block]:
    widths = _column_widths(lines, body_size)
    lines = _drop_figure_text(lines, body_size, widths)
    headings = _heading_flags(lines, body_size)
    # 只出現一次的字級多半是期刊名或刊頭，不列入章節分級
    size_counts = Counter(_size_key(l) for l, h in zip(lines, headings) if h)
    heading_sizes = sorted((s for s, n in size_counts.items() if n >= 2), reverse=True)

    blocks: list[Block] = []
    paragraph: list[_Line] = []
    in_references = False
    # 參考文獻跨段收集，最後依編號切；每筆的換行常被誤判為分段
    references: list[list[_Line]] = []

    def flush_paragraph() -> None:
        if paragraph:
            if in_references:
                references.append(list(paragraph))
            else:
                blocks.append(Block("", _join([l.text for l in paragraph])))
            paragraph.clear()

    def flush_references() -> None:
        flush_paragraph()
        if references:
            blocks.extend(Block(REFERENCE, t) for t in _split_references(references))
            references.clear()

    i = 0
    while i < len(lines):
        line = lines[i]

        if headings[i]:
            flush_references()
            # 連續的標題行（標題換行）併成一個
            parts = [line.text]
            while (
                i + 1 < len(lines)
                and headings[i + 1]
                and abs(lines[i + 1].size - line.size) < 0.5
                and lines[i + 1].bold == line.bold
                and lines[i + 1].page == line.page
                and 0 <= lines[i + 1].top - lines[i].bottom < line.size * 0.6
            ):
                i += 1
                parts.append(lines[i].text)
            text = _join(parts)
            level = _heading_level(line, text, heading_sizes)
            if REFERENCES_TITLE.match(text):
                in_references = True
                level = HEADING
            elif level == HEADING:
                # 參考文獻之後的章（附錄等）不算參考文獻
                in_references = False
            blocks.append(Block(level, text))
            i += 1
            continue

        # 表格：表格標題之後的表格列，或連續三行以上的表格列
        if TABLE_CAPTION.match(line.text) or _starts_table_run(lines, i):
            flush_references()
            # 前一個表格沒有標題：標題在表格下方，不往下收表格列
            below = bool(blocks) and blocks[-1].kind == TABLE and not _has_caption(blocks[-1])
            caption, rows, i = _read_table(lines, i, body_size, widths, rows=not below)
            if rows:
                blocks.append(Block(TABLE, "\n".join(([caption] if caption else []) + rows)))
            elif caption and blocks and blocks[-1].kind == TABLE and not _has_caption(blocks[-1]):
                # 表格標題在表格下方（常見於電腦科學論文），接回前一個表格
                blocks[-1].text = caption + "\n" + blocks[-1].text
            elif caption:
                blocks.append(Block("", caption))
            continue

        if paragraph and _breaks_paragraph(paragraph[-1], line, widths):
            flush_paragraph()
        paragraph.append(line)
        i += 1

    flush_references()
    return [b for b in blocks if b.text.strip()]


def _drop_figure_text(lines: list[_Line], body_size: float, widths: dict) -> list[_Line]:
    """移除向量圖內的文字（座標軸、圖例、流程圖方塊）。

    圖內文字是零碎的短行：遠小於內文的短行、只有數字的行，
    或連續三行以上不成句的短行。表格列有欄距（gaps），不在此列。
    """

    def short(line: _Line) -> bool:
        return (
            line.x1 - line.x0 < widths.get(_column_key(line), 300) * 0.4
            and (line.gaps < 2 or NUMBERS_ONLY.match(line.text) is not None)
            # 比內文大或粗體的短行是章節標題；「B)」這類圖的分圖編號除外
            and (
                (line.size <= body_size * 1.05 and not line.bold)
                or len(re.findall(r"[A-Za-z\u4e00-\u9fff]", line.text)) < 2
            )
            and not SENTENCE_END.search(line.text)
            and not TABLE_CAPTION.match(line.text)
            and not FIGURE_CAPTION.match(line.text)
        )

    drop = set()
    for i, line in enumerate(lines):
        if short(line) and line.size < body_size * 0.75:
            drop.add(i)
        elif NUMBERS_ONLY.match(line.text) and line.gaps < 2:
            drop.add(i)

    i = 0
    while i < len(lines):
        j = i
        while j < len(lines) and (short(lines[j]) or j in drop):
            j += 1
        if j - i >= 3:
            drop.update(range(i, j))
        i = max(j, i + 1)
    return [line for i, line in enumerate(lines) if i not in drop]


def _heading_flags(lines: list[_Line], body_size: float) -> list[bool]:
    """逐行判斷是否為章節標題。

    第一頁摘要之前是期刊名、作者、單位等資訊，即使字大或粗體也不算章節。
    """
    abstract = next(
        (
            i
            for i, l in enumerate(lines)
            if l.page <= 1 and ABSTRACT_TITLE.match(l.text.strip().rstrip(":："))
        ),
        None,
    )
    columns = _page_columns(lines)

    flags = []
    for i, line in enumerate(lines):
        if abstract is not None and i < abstract:
            flags.append(False)
            continue
        if i == abstract:
            flags.append(True)
            continue
        flag = _looks_like_heading(line, body_size)
        # 標題對齊欄的左緣或在欄內置中；圖內的座標軸名稱、圖例不會
        if flag and not _aligned(line, columns.get(line.page, [])):
            flag = False
        # 只靠字級大（非粗體、斜體）的標題，上方要有空白；內文行混到大字號符號時會誤判
        prev = lines[i - 1] if i else None
        if (
            flag
            and not (line.bold or line.italic)
            and prev is not None
            and prev.page == line.page
            and abs(prev.x0 - line.x0) < line.size * 4
            and line.top - prev.bottom < line.size * 0.4
        ):
            flag = False
        # 下一行小寫開頭表示句子還沒完：段落開頭的粗體字或圖說，不是標題
        nxt = lines[i + 1] if i + 1 < len(lines) else None
        if flag and nxt and nxt.text[:1].islower():
            flag = False
        flags.append(flag)
    return flags


def _page_columns(lines: list[_Line]) -> dict[int, list[tuple[float, float]]]:
    """各頁內文欄的 (左緣, 寬度)，取自夠長的內文行。"""
    edges: dict[int, dict[int, list[_Line]]] = {}
    for line in lines:
        if len(line.text) >= 40 and line.gaps < 2:
            edges.setdefault(line.page, {}).setdefault(round(line.x0 / 6), []).append(line)
    columns: dict[int, list[tuple[float, float]]] = {}
    for page, groups in edges.items():
        columns[page] = [
            (min(l.x0 for l in group), statistics.median(l.x1 - l.x0 for l in group))
            for group in groups.values()
            if len(group) >= 2
        ]
    return columns


def _aligned(line: _Line, columns: list[tuple[float, float]]) -> bool:
    if not columns:
        return True
    center = (line.x0 + line.x1) / 2
    return any(
        abs(line.x0 - left) < line.size or abs(center - (left + width / 2)) < line.size
        for left, width in columns
    )


def _looks_like_heading(line: _Line, body_size: float) -> bool:
    text = line.text
    if len(text) > 120 or len(text.split()) > 15 or len(text) < 2:
        return False
    if re.search(r"[.,;]$", text):
        return False
    if not text[0].isalnum() or text[0].islower() or line.gaps >= 2:
        return False
    # 至少要有兩個文字；「×3」這類公式符號不是標題
    if len(re.findall(r"[A-Za-z\u4e00-\u9fff]", text)) < 2:
        return False
    if TABLE_CAPTION.match(text) or FIGURE_CAPTION.match(text):
        return False
    # 句中有句點接大寫：段落開頭的粗體小標接內文（如「Plain Networks. We first」）
    if re.search(r"[a-z]{2,}\. [A-Z]", text):
        return False
    # 結尾是連接詞：句子換行，不是標題
    if re.search(r"(&|\b(and|or|of|the|in|for|with|to|on|by|a|an))$", text, re.I):
        return False
    # 作者名單：逗號分隔的人名
    if text.count(",") >= 2:
        return False
    if line.size < body_size * 0.85:
        return False
    return (
        line.bold
        or line.size >= body_size * 1.12
        # 斜體小節標題（如 BMC 期刊）；須是獨立短行
        or (line.italic and len(text.split()) <= 8)
        or (text.isupper() and REFERENCES_TITLE.match(text) is not None)
    )


def _heading_level(line: _Line, text: str, heading_sizes: list[float]) -> str:
    """字級最大的一級為章，其餘為節；「2.1」這類編號與斜體標題一律為節。"""
    if SUBSECTION_NUMBER.match(text) or (line.italic and not line.bold):
        return SUBHEADING
    if not heading_sizes:
        return HEADING
    # 與最大的一級相差不到 0.5 視為章
    return HEADING if _size_key(line) >= heading_sizes[0] - 0.5 else SUBHEADING


def _column_key(line: _Line) -> tuple[int, int]:
    return (line.page, round(line.x0 / 20))


def _column_widths(lines: list[_Line], body_size: float) -> dict[tuple[int, int], float]:
    """各頁各欄的內文行寬（第 90 百分位），判斷一行是否寫滿。"""
    groups: dict[tuple[int, int], list[float]] = {}
    for line in lines:
        # 只用夠長的內文行；圖內短標籤會讓欄寬估得太窄
        if abs(line.size - body_size) <= 1 and line.gaps < 2 and len(line.text) >= 40:
            groups.setdefault(_column_key(line), []).append(line.x1 - line.x0)
    widths: dict[tuple[int, int], float] = {}
    for key, values in groups.items():
        values.sort()
        widths[key] = values[max(int(len(values) * 0.9) - 1, 0)]
    # 段首縮排的行會落到旁邊的欄位鍵，沿用鄰近欄的寬度
    for (page, col), width in list(widths.items()):
        for neighbour in (col - 1, col + 1):
            widths.setdefault((page, neighbour), width)
    return widths


def _breaks_paragraph(prev: _Line, line: _Line, widths: dict) -> bool:
    """判斷 line 是否開始新段落。"""
    # 字級或字型明顯不同：圖說、註腳與內文之間
    if abs(prev.size - line.size) > max(prev.size, line.size) * 0.15:
        return True
    same_column = (
        line.page == prev.page and abs(line.x0 - prev.x0) < prev.size * 4 and line.top > prev.top
    )
    if same_column:
        # 一般行距下，上一行下緣到這一行上緣幾乎沒有空隙；段落間會多空一段
        if line.top - prev.bottom > prev.size * 0.9:
            return True
        indent = line.x0 - prev.x0
        if prev.size * 0.8 < indent < prev.size * 4 and SENTENCE_END.search(prev.text):
            return True
    width = widths.get(_column_key(prev), prev.x1 - prev.x0)
    short = prev.last_x1 - prev.x0 < width * 0.85
    return short and bool(SENTENCE_END.search(prev.text))


def _starts_table_run(lines: list[_Line], index: int) -> bool:
    run = lines[index : index + 3]
    return len(run) == 3 and all(l.gaps >= 2 for l in run)


def _read_table(
    lines: list[_Line], index: int, body_size: float, widths: dict, *, rows: bool = True
) -> tuple[str, list[str], int]:
    """讀取表格：先收表格標題（可能多行），再收表格列。

    Args:
        rows: 是否收表格列；標題在表格下方時只收標題。

    Returns:
        (表格標題, 表格列, 下一個未處理的行)。標題在表格下方時表格列為空。
    """
    caption = ""
    # 有表格標題時，兩欄的表格只有一個欄距也算表格列
    min_gaps = 2
    if TABLE_CAPTION.match(lines[index].text):
        min_gaps = 1
        parts = [lines[index]]
        index += 1
        # 表格標題到第一個表格列為止；最多六行，避免沒有表格列時吃掉內文
        while (
            index < len(lines)
            and lines[index].gaps < 1
            and len(parts) < 6
            and not _breaks_paragraph(parts[-1], lines[index], widths)
        ):
            parts.append(lines[index])
            index += 1
        caption = _join([l.text for l in parts])

    collected: list[str] = []
    while rows and index < len(lines):
        line = lines[index]
        width = widths.get(_column_key(line), 300)
        if line.gaps >= min_gaps:
            collected.append(_clean(line.text))
        # 表格內換行的儲存格（如單位）：表格列之間的短行
        elif collected and line.x1 - line.x0 < width * 0.45 and line.size <= body_size * 1.02:
            collected.append(_clean(line.text))
        else:
            break
        index += 1
    return caption, collected, index


def _has_caption(block: Block) -> bool:
    return bool(TABLE_CAPTION.match(block.text))


def _split_references(paragraphs: list[list[_Line]]) -> list[str]:
    """參考文獻每筆一段：多數筆以編號開頭時依編號切，否則沿用分段。"""
    lines = [line for paragraph in paragraphs for line in paragraph]
    starts = [i for i, l in enumerate(lines) if REFERENCE_START.match(l.text)]
    if len(starts) < max(2, len(paragraphs) // 2):
        return [_join([l.text for l in p]) for p in paragraphs]
    if starts[0] != 0:
        starts.insert(0, 0)
    return [_join([l.text for l in lines[a:b]]) for a, b in zip(starts, starts[1:] + [len(lines)])]


# ── 文字處理 ──────────────────────────────────────


def _join(parts: list[str]) -> str:
    """把多行接成一段：行尾斷字直接相連，中文之間不加空白。"""
    text = ""
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if not text:
            text = part
        elif text.endswith(SOFT_HYPHEN):
            text = text[:-1] + part
        elif CJK.search(text[-1]) or CJK.search(part[0]):
            text += part
        else:
            text += " " + part
    return _clean(text)


def _clean(text: str) -> str:
    # 行中的斷字標記是原文的連字號（如 Mary-Hardin、P-value）
    text = text.replace(SOFT_HYPHEN, "-")
    # 數學字型的括號等符號對應到私用區，顯示不出來
    text = CONTROL.sub("", PRIVATE_USE.sub("", text))
    return re.sub(r"[ \t]+", " ", text).strip()
