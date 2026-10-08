#!/usr/bin/env python3
"""論文 PDF 原檔的上傳、列出與下載（Supabase Storage 的 papers bucket）。

需要 .env 的 SUPABASE_URL 與 SUPABASE_SECRET_KEY。
下載預設存到專案根目錄的 papers/（不進版控）。

用法：
    ./venv/bin/python scripts/paper.py upload 論文.pdf [--source 下載來源網址]
    ./venv/bin/python scripts/paper.py list
    ./venv/bin/python scripts/paper.py download <guid> [--out 目錄]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from podscript import storage  # noqa: E402

DOWNLOAD_DIR = ROOT / "papers"


def main() -> None:
    load_dotenv(ROOT / ".env")

    parser = argparse.ArgumentParser(description="論文 PDF 原檔管理")
    commands = parser.add_subparsers(dest="command", required=True)

    upload = commands.add_parser("upload", help="上傳 PDF")
    upload.add_argument("pdf", type=Path)
    upload.add_argument("--source", help="論文的下載來源網址")

    commands.add_parser("list", help="列出已上傳的 PDF")

    download = commands.add_parser("download", help="下載 PDF")
    download.add_argument("guid")
    download.add_argument("--out", type=Path, default=DOWNLOAD_DIR)

    args = parser.parse_args()
    try:
        if args.command == "upload":
            guid = storage.upload_paper(args.pdf, source_url=args.source)
            print(f"已上傳：{guid}（{args.pdf.name}）")
        elif args.command == "list":
            for paper in storage.list_papers():
                print(
                    f"{paper.guid}  {paper.size / 1024 / 1024:5.1f} MB  "
                    f"{paper.metadata.get('filename', '')}  "
                    f"{paper.metadata.get('source_url', '')}"
                )
        else:
            dest = storage.download_paper(args.guid, args.out / f"{args.guid}.pdf")
            print(f"已下載：{dest}")
    except storage.StorageError as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    main()
