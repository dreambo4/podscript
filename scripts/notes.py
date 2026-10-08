#!/usr/bin/env python3
"""思辨練習紀錄（notes/*.md）與 Supabase Storage 的 notes bucket 雙向同步。

逐份比對內容 MD5：相同就略過；不同時比較「本機修改時間」與「Storage 最後上傳時間」，
較新的一方覆蓋另一方。兩台電腦都改過同一份時，以較晚修改的為準。
只有一方有的檔案直接複製到另一方；不會刪除任何一方的檔案。

需要 .env 的 SUPABASE_URL 與 SUPABASE_SECRET_KEY。

用法：
    ./venv/bin/python scripts/notes.py sync   # 雙向同步
    ./venv/bin/python scripts/notes.py pull   # 只下載
    ./venv/bin/python scripts/notes.py push   # 只上傳
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from podscript import storage  # noqa: E402

NOTES_DIR = ROOT / "notes"


def plan(remote: dict[str, storage.StoredNote]) -> tuple[list[str], list[Path]]:
    """回傳要下載的物件名稱與要上傳的本機檔案。"""
    to_pull: list[str] = []
    to_push: list[Path] = []
    local = {path.name: path for path in NOTES_DIR.glob("*.md")}

    for name in sorted(remote.keys() | local.keys()):
        note, path = remote.get(name), local.get(name)
        if path is None:
            to_pull.append(name)
        elif note is None:
            to_push.append(path)
        elif hashlib.md5(path.read_bytes()).hexdigest() == note.md5:
            continue
        elif path.stat().st_mtime > note.updated_at.timestamp():
            to_push.append(path)
        else:
            to_pull.append(name)
    return to_pull, to_push


def main() -> None:
    load_dotenv(ROOT / ".env")

    parser = argparse.ArgumentParser(description="思辨練習紀錄同步")
    parser.add_argument("command", choices=["sync", "pull", "push"])
    args = parser.parse_args()

    NOTES_DIR.mkdir(exist_ok=True)
    try:
        remote = {note.name: note for note in storage.list_notes()}
        to_pull, to_push = plan(remote)
        if args.command == "push":
            to_pull = []
        elif args.command == "pull":
            to_push = []

        for name in to_pull:
            path = storage.download_note(name, NOTES_DIR / name)
            # 修改時間對齊上傳時間，之後在本機編輯才會被判定為本機較新。
            timestamp = remote[name].updated_at.timestamp()
            os.utime(path, (timestamp, timestamp))
            print(f"下載：{name}")
        for path in to_push:
            storage.upload_note(path)
            print(f"上傳：{path.name}")
    except storage.StorageError as exc:
        sys.exit(str(exc))

    total = len(list(NOTES_DIR.glob("*.md")))
    print(f"下載 {len(to_pull)} 份、上傳 {len(to_push)} 份，本機共 {total} 份")


if __name__ == "__main__":
    main()
