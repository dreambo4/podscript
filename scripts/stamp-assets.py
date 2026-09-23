#!/usr/bin/env python3
"""把 mobile-web/index.html 與 sw.js 的資源版本改成檔案修改時間。

Firebase Hosting 是純靜態部署，沒有伺服器端可即時產生版本號，
故在部署前先戳一次。手動維護 ?v=N 容易忘記更新，
導致改了樣式卻看到舊畫面。

用法：
    python3 scripts/stamp-assets.py        # 戳章
    python3 scripts/stamp-assets.py --check # 只檢查是否需要戳章（CI 用）
"""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "mobile-web"

# index.html 中需要帶版本的資源。
# mindmap-render.js 以 ESM import 載入（import 路徑不能帶 ?v=，否則會被當成不同模組、
# 也對不上 SW 快取的 URL），故它不會被 stamp_index 加上 ?v=；但仍列入是為了讓它的
# 內容變動能計入 sw.js 的 CACHE_VERSION，觸發 SW 更新（見 stamp_sw）。
ASSETS = ("style.css", "app.js", "mindmap-render.js")


def stamp_index() -> tuple[str, bool]:
    """回傳 (新內容, 是否有變動)。"""
    path = ROOT / "index.html"
    html = path.read_text(encoding="utf-8")
    original = html

    for name in ASSETS:
        # 版本取自檔案內容而非 mtime：git clone、checkout 都會刷新 mtime，
        # 內容沒變卻產生新版本會讓使用者白白重抓。
        stamp = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()[:12]
        # 同時吃 "app.js" 與 "app.js?v=舊值" 兩種寫法
        html = re.sub(
            rf'"{re.escape(name)}(\?v=[^"]*)?"',
            f'"{name}?v={stamp}"',
            html,
        )

    return html, html != original


def stamp_sw(index_html: str) -> tuple[str, bool]:
    """Service Worker 的快取版本一併更新。

    外殼檔案改了但 CACHE_VERSION 沒動，SW 會繼續回舊快取，
    使用者即使重新整理也拿不到新版。

    版本取自外殼內容的雜湊而非 mtime：戳章本身會改寫 index.html，
    用 mtime 會讓每次執行都產生新版本，永遠不收斂。

    Args:
        index_html: 戳章後的 index.html 內容（尚未寫入檔案）。
    """
    path = ROOT / "sw.js"
    code = path.read_text(encoding="utf-8")

    digest = hashlib.sha256()
    digest.update(index_html.encode("utf-8"))
    for name in ASSETS:
        digest.update((ROOT / name).read_bytes())

    updated = re.sub(
        r'const CACHE_VERSION = "podscript-v[^"]*";',
        f'const CACHE_VERSION = "podscript-v{digest.hexdigest()[:12]}";',
        code,
    )
    return updated, updated != code


def main() -> int:
    check_only = "--check" in sys.argv

    index_html, index_changed = stamp_index()
    results = [
        (ROOT / "index.html", index_html, index_changed),
        (ROOT / "sw.js", *stamp_sw(index_html)),
    ]

    changed = [path for path, _content, is_changed in results if is_changed]

    if check_only:
        if changed:
            print("需要重新戳章：" + "、".join(p.name for p in changed))
            return 1
        print("版本已是最新")
        return 0

    for path, content, is_changed in results:
        if is_changed:
            path.write_text(content, encoding="utf-8")
            print(f"已更新 {path.name}")

    if not changed:
        print("版本已是最新，未變動")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
