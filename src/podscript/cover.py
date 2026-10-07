"""內容封面：摘要模型依本集內容畫的線條插圖。

封面存成 {"svg": "...", "color": "#rrggbb"}：
- svg 為 viewBox 0 0 48 48 的 SVG 內部元素（不含 <svg> 外層）
- 線條與填色一律 currentColor，前端依 color 與深淺色主題上色

模型輸出的 SVG 不可信，存檔前以白名單重建：只留繪圖元素與幾何、樣式屬性，
顏色只允許 none／currentColor，其餘（script、事件屬性、外部連結、url() 等）一律捨棄。
手機前端顯示前會再過濾一次。
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

# 封面規則，摘要 prompt 與只重畫封面的 prompt 共用。
COVER_RULES = """cover 規則：
- 一個代表本集最核心主題的封面插圖，用具體、一眼能認出的物件（例如房子、望遠鏡、狐狸、月琴），1 到 3 個物件組合，不要抽象幾何圖形
- 不可包含任何文字、字母、數字，也不要用線條拼出文字或符號
- svg 為 viewBox="0 0 48 48" 的 SVG 內部元素，不含外層 <svg> 標籤；只能使用 path、circle、ellipse、rect、line、polyline、polygon、g
- 線條插圖風格：主要以 stroke="currentColor" fill="none" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" 描線；
  可用 fill="currentColor" fill-opacity="0.18" 做淡色填面，小圓點可用 fill="currentColor"
- 顏色一律寫 currentColor，不可寫色碼
- 主體置中，約佔 40×40 範圍，四周留白；元素總數不超過 25 個
- color 為一個 #RRGGBB 主色，中等飽和度、偏柔和，在白色與深色背景上都看得清楚"""

# 摘要 prompt 輸出格式中 cover 欄位的範例
COVER_EXAMPLE = (
    '"cover": {{"svg": "<path d=\\"M9 19 19 10 29 19V39H9z\\" fill=\\"none\\" stroke=\\"currentColor\\" '
    'stroke-width=\\"2.2\\" stroke-linecap=\\"round\\" stroke-linejoin=\\"round\\"/>", "color": "#3f8a74"}}'
)

DEFAULT_COLOR = "#7a8296"
MAX_SVG_LENGTH = 8000
MAX_ELEMENTS = 40

ALLOWED_TAGS = {"path", "circle", "ellipse", "rect", "line", "polyline", "polygon", "g"}

# 屬性白名單與值的格式
_NUMBER = r"-?\d*\.?\d+(?:e-?\d+)?"
_NUMBER_RE = re.compile(rf"^{_NUMBER}$", re.I)
_PATH_RE = re.compile(r"^[MmLlHhVvCcSsQqTtAaZz0-9eE.,\s+-]+$")
_POINTS_RE = re.compile(r"^[0-9eE.,\s+-]+$")
_TRANSFORM_RE = re.compile(
    rf"^(\s*(rotate|translate|scale)\(\s*{_NUMBER}(\s*[,\s]\s*{_NUMBER}){{0,2}}\s*\)\s*)+$", re.I
)
_PAINT_VALUES = {"none", "currentcolor"}
_ENUM_VALUES = {
    "stroke-linecap": {"butt", "round", "square"},
    "stroke-linejoin": {"miter", "round", "bevel"},
    "fill-rule": {"nonzero", "evenodd"},
}
_NUMERIC_ATTRS = {
    "cx", "cy", "r", "rx", "ry", "x", "y", "width", "height",
    "x1", "y1", "x2", "y2", "stroke-width", "opacity", "fill-opacity", "stroke-opacity",
}
_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def normalize(raw: object) -> dict | None:
    """檢查模型回傳的封面並以白名單重建 SVG。

    封面是附加資訊，格式不對時回傳 None，不影響摘要。

    Returns:
        {"svg": 過濾後的內部元素, "color": "#rrggbb"}；沒有可用的繪圖元素時為 None。
    """
    if not isinstance(raw, dict):
        return None
    svg = sanitize_svg(raw.get("svg"))
    if not svg:
        return None
    color = raw.get("color")
    return {
        "svg": svg,
        "color": color.lower() if isinstance(color, str) and _HEX_COLOR_RE.match(color) else DEFAULT_COLOR,
    }


def sanitize_svg(markup: object) -> str | None:
    """只保留白名單內的元素與屬性，重新序列化。

    Returns:
        過濾後的 SVG 內部元素字串；無法解析或沒有剩下任何繪圖元素時為 None。
    """
    if not isinstance(markup, str) or not markup.strip() or len(markup) > MAX_SVG_LENGTH:
        return None
    # DOCTYPE／ENTITY 宣告可能用於實體展開攻擊，直接拒絕
    if "<!" in markup or "<?" in markup:
        return None

    # 模型偶爾仍會帶外層 <svg>，包一層後統一從根節點的子元素開始處理
    try:
        root = ET.fromstring(f"<root>{markup}</root>")
    except ET.ParseError:
        return None
    if len(root) == 1 and _local_name(root[0].tag) == "svg":
        root = root[0]

    count = 0

    def build(node: ET.Element) -> str:
        nonlocal count
        tag = _local_name(node.tag)
        if tag not in ALLOWED_TAGS or count >= MAX_ELEMENTS:
            return ""
        count += 1
        attrs = "".join(
            f' {name}="{value}"'
            for name, value in (
                (_local_name(k), v.strip()) for k, v in node.attrib.items()
            )
            if _attr_allowed(name, value)
        )
        if tag == "g":
            children = "".join(build(child) for child in node)
            return f"<g{attrs}>{children}</g>" if children else ""
        return f"<{tag}{attrs}/>"

    svg = "".join(build(child) for child in root)
    return svg or None


def _attr_allowed(name: str, value: str) -> bool:
    if name in ("fill", "stroke"):
        return value.lower() in _PAINT_VALUES
    if name in _NUMERIC_ATTRS:
        return bool(_NUMBER_RE.match(value))
    if name == "d":
        return bool(_PATH_RE.match(value))
    if name == "points":
        return bool(_POINTS_RE.match(value))
    if name == "transform":
        return bool(_TRANSFORM_RE.match(value))
    if name == "stroke-dasharray":
        return bool(_POINTS_RE.match(value))
    if name in _ENUM_VALUES:
        return value.lower() in _ENUM_VALUES[name]
    return False


def _local_name(tag: str) -> str:
    """去掉 ElementTree 的命名空間前綴：{http://www.w3.org/2000/svg}path → path。"""
    return tag.rsplit("}", 1)[-1].lower()
