"""上傳結果至 Supabase，並在上傳後以資料庫為單一資料來源。

只送輕量資料（逐字稿、摘要、心智圖、標籤），音檔留本機。
以 episode_guid 為鍵做 upsert，同一集重複上傳為更新而非新增。

走 Postgres 直連而非 REST API：上傳是本機單次寫入，
用 SQL 比繞過 PostgREST 直接，也與 exercise-together 的做法一致。

上傳成功後本機目錄整個清除，該集之後一律由 fetch_episode() 從資料庫還原；
本機 audio/ 僅作為處理中的暫存區，換機時不需搬移。
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from pathlib import Path

import psycopg

from . import diarize, pipeline
from .resolvers import Episode

CONNECT_TIMEOUT = 20

UPSERT_SQL = """
insert into episodes (
    platform, source_url, episode_guid, podcast_name, title,
    published_at, duration_sec, summary, mindmap_mermaid, hashtags,
    transcript, speakers, provenance, transcript_text
) values (
    %(platform)s, %(source_url)s, %(episode_guid)s, %(podcast_name)s, %(title)s,
    %(published_at)s, %(duration_sec)s, %(summary)s, %(mindmap_mermaid)s, %(hashtags)s,
    %(transcript)s, %(speakers)s, %(provenance)s, %(transcript_text)s
)
on conflict (episode_guid) do update set
    source_url      = excluded.source_url,
    podcast_name    = excluded.podcast_name,
    title           = excluded.title,
    published_at    = excluded.published_at,
    duration_sec    = excluded.duration_sec,
    summary         = excluded.summary,
    mindmap_mermaid = excluded.mindmap_mermaid,
    hashtags        = excluded.hashtags,
    transcript      = excluded.transcript,
    speakers        = excluded.speakers,
    provenance      = excluded.provenance,
    transcript_text = excluded.transcript_text,
    updated_at      = now()
returning id, (xmax = 0) as inserted
"""


class UploadError(Exception):
    """上傳失敗。訊息需可直接顯示給使用者。"""


def upload(directory: Path, *, discard: bool = True) -> dict:
    """把單集結果寫入 Supabase，成功後清除整個本機目錄。

    上傳代表使用者已認可這份逐字稿，資料庫的 transcript 與 speakers
    足以還原顯示、重新生成摘要與改說話者名稱，故本機不再留任何檔案。

    Args:
        directory: 單集的工作目錄。
        discard: 是否於上傳成功後清除本機檔案。

    Returns:
        含 id、是否為新增、資料大小與釋出空間的結果。

    Raises:
        UploadError: 未設定 DATABASE_URL、尚未處理完成，或資料庫寫入失敗。
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise UploadError("未設定 DATABASE_URL，無法上傳")

    payload = build_payload(directory)

    try:
        with psycopg.connect(url, connect_timeout=CONNECT_TIMEOUT) as conn:
            with conn.cursor() as cur:
                cur.execute(UPSERT_SQL, payload)
                row = cur.fetchone()
    except psycopg.Error as exc:
        raise UploadError(f"寫入資料庫失敗：{exc}") from exc

    episode_id, inserted = row
    # 寫入成功才刪，失敗時檔案仍在，可重試上傳。
    freed = _remove_local(directory) if discard else 0

    return {
        "id": str(episode_id),
        "inserted": bool(inserted),
        "bytes": len(payload["transcript"]) + len(payload["transcript_text"] or ""),
        "freed_bytes": freed,
    }


def discard_audio(directory: Path) -> dict:
    """手動清除該集的本機檔案。

    上傳時已自動清除，此函式供未自動刪除的舊資料使用。

    Raises:
        UploadError: 尚未上傳至 Supabase。
    """
    if not uploaded_at(directory.name):
        raise UploadError("尚未上傳至 Supabase，不可刪除本機檔案")
    return {"freed_bytes": _remove_local(directory)}


def _remove_local(directory: Path) -> int:
    """清除該集的整個本機目錄，回傳釋出的位元組數。

    含音檔與所有中間產物：mp3 網址帶時效性參數，刪除後無法重新下載，
    故轉錄與說話者分離的快取留著也無法重跑，一併清除。
    """
    if not directory.exists():
        return 0

    freed = sum(p.stat().st_size for p in directory.rglob("*") if p.is_file())
    shutil.rmtree(directory, ignore_errors=True)
    return freed


def build_payload(directory: Path) -> dict:
    """組出要寫入的資料。

    本機已清除的單集改由資料庫取回，讓已上傳者仍可再次上傳
    （例如改過說話者名稱或重新生成摘要後）。

    Raises:
        UploadError: 本機與資料庫都沒有該集的處理結果。
    """
    result = pipeline.load_result(directory)
    summary = pipeline.load_summary(directory) or {}

    if result is None:
        fetched = fetch_episode(directory.name)
        if fetched is None:
            raise UploadError("尚未完成處理，無法上傳")
        result, summary = fetched

    episode = result.episode
    segments = [seg.to_dict() for seg in result.segments]

    return {
        "platform": episode.platform,
        "source_url": episode.source_url,
        "episode_guid": episode.episode_guid,
        "podcast_name": episode.podcast_name,
        "title": episode.title,
        "published_at": episode.published_at,
        "duration_sec": episode.duration_sec,
        "summary": summary.get("summary"),
        "mindmap_mermaid": summary.get("mindmap"),
        "hashtags": summary.get("hashtags", []),
        "transcript": json.dumps(segments, ensure_ascii=False),
        "speakers": json.dumps(result.speakers, ensure_ascii=False),
        "provenance": json.dumps(result.provenance.to_dict(), ensure_ascii=False),
        # jsonb 無法建 trgm 索引，另存攤平的純文字供中文搜尋
        "transcript_text": "".join(seg.text for seg in result.segments),
    }


FETCH_COLUMNS = """
    platform, source_url, episode_guid, podcast_name, title,
    published_at, duration_sec, summary, mindmap_mermaid, hashtags,
    transcript, speakers, provenance, updated_at, created_at
"""


def fetch_episode(episode_guid: str) -> tuple[pipeline.Result, dict] | None:
    """從資料庫取回單集，還原成 Result 與摘要。

    上傳後本機已無檔案，顯示、改名與重新生成摘要都改由此還原。

    Returns:
        (Result, 摘要 dict)；查無此集或未設定 DATABASE_URL 時為 None。
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        return None

    try:
        with psycopg.connect(url, connect_timeout=CONNECT_TIMEOUT) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"select {FETCH_COLUMNS} from episodes where episode_guid = %s",
                    (episode_guid,),
                )
                row = cur.fetchone()
    except psycopg.Error as exc:
        raise UploadError(f"讀取資料庫失敗：{exc}") from exc

    return _row_to_result(row) if row else None


def fetch_all() -> list[tuple[pipeline.Result, dict, str, str]]:
    """取回所有已上傳的單集，供列表頁在本機無檔案時顯示。

    Returns:
        (Result, 摘要, 上傳時間, 建立時間) 的列表；查詢失敗時為空列表。
        列表頁不應因資料庫暫時不可用而整頁失敗，故不拋例外。
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        return []

    try:
        with psycopg.connect(url, connect_timeout=CONNECT_TIMEOUT) as conn:
            with conn.cursor() as cur:
                cur.execute(f"select {FETCH_COLUMNS} from episodes")
                rows = cur.fetchall()
    except psycopg.Error:
        return []

    items = []
    for row in rows:
        result, summary = _row_to_result(row)
        items.append((result, summary, _iso(row[13]), _iso(row[14])))
    return items


def _row_to_result(row: tuple) -> tuple[pipeline.Result, dict]:
    """把資料庫的一列還原成 Result 與摘要。

    mp3_url 與 description 未存入資料庫（音檔網址帶時效性、簡介未使用），
    還原時留空；下游僅用於顯示，不影響功能。
    """
    (
        platform, source_url, episode_guid, podcast_name, title,
        published_at, duration_sec, summary, mindmap, hashtags,
        transcript, speakers, provenance, *_timestamps,
    ) = row

    episode = Episode(
        platform=platform,
        source_url=source_url or "",
        episode_guid=episode_guid,
        podcast_name=podcast_name or "",
        title=title or "",
        mp3_url="",
        duration_sec=duration_sec,
        published_at=published_at,
    )
    result = pipeline.Result(
        episode=episode,
        segments=[
            diarize.DiarizedSegment(**seg) for seg in _as_json(transcript, [])
        ],
        speakers=_as_json(speakers, {}),
        provenance=pipeline.Provenance(**_as_json(provenance, {})),
    )
    return result, {
        "summary": summary,
        "mindmap": mindmap,
        "hashtags": list(hashtags or []),
    }


def _as_json(value, default):
    """psycopg 依欄位型別回傳 dict/list 或字串，兩者都要能讀。"""
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    return json.loads(value)


def _iso(value) -> str:
    return value.isoformat() if isinstance(value, datetime) else str(value or "")


def update_episode(
    episode_guid: str,
    *,
    speakers: dict[str, str] | None = None,
    summary: dict | None = None,
    transcript_text: str | None = None,
) -> None:
    """更新已上傳單集的指定欄位。

    改說話者名稱與重新生成摘要在上傳後直接寫回資料庫，
    不需要本機檔案，手機端因此也能改。

    Raises:
        UploadError: 未設定 DATABASE_URL 或寫入失敗。
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise UploadError("未設定 DATABASE_URL，無法更新")

    sets, params = [], {"guid": episode_guid}
    if speakers is not None:
        sets.append("speakers = %(speakers)s")
        params["speakers"] = json.dumps(speakers, ensure_ascii=False)
    if transcript_text is not None:
        sets.append("transcript_text = %(transcript_text)s")
        params["transcript_text"] = transcript_text
    if summary is not None:
        sets += [
            "summary = %(summary)s",
            "mindmap_mermaid = %(mindmap)s",
            "hashtags = %(hashtags)s",
        ]
        params |= {
            "summary": summary.get("summary"),
            "mindmap": summary.get("mindmap"),
            "hashtags": summary.get("hashtags", []),
        }
    if not sets:
        return

    sets.append("updated_at = now()")
    try:
        with psycopg.connect(url, connect_timeout=CONNECT_TIMEOUT) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"update episodes set {', '.join(sets)}"
                    " where episode_guid = %(guid)s",
                    params,
                )
    except psycopg.Error as exc:
        raise UploadError(f"更新資料庫失敗：{exc}") from exc


def uploaded_at(episode_guid: str) -> str | None:
    """查詢該集上次上傳的時間，供介面顯示。回傳 None 表示尚未上傳。"""
    return uploaded_times([episode_guid]).get(episode_guid)


def uploaded_times(episode_guids: list[str]) -> dict[str, str]:
    """一次查詢多集的上傳時間。

    列表頁會一次顯示所有單集，逐集連線會讓每次載入都開數十條連線。

    Returns:
        已上傳者的 {guid: 時間}；未上傳或查詢失敗者不在其中。
    """
    url = os.environ.get("DATABASE_URL")
    if not url or not episode_guids:
        return {}

    try:
        with psycopg.connect(url, connect_timeout=CONNECT_TIMEOUT) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "select episode_guid, updated_at from episodes"
                    " where episode_guid = any(%s)",
                    (list(episode_guids),),
                )
                rows = cur.fetchall()
    except psycopg.Error:
        return {}

    return {guid: updated.isoformat() for guid, updated in rows}


def fetch_existing_hashtags() -> list[str]:
    """取得所有已上傳集數用過的標籤，供生成新標籤時比對是否同義。

    查詢失敗或未設定 DATABASE_URL 時回傳空列表，呼叫端應視同「無標籤庫可比對」，
    不可讓標籤收斂的失敗擋住摘要生成。
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        return []

    try:
        with psycopg.connect(url, connect_timeout=CONNECT_TIMEOUT) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "select distinct unnest(hashtags) from episodes"
                    " where hashtags is not null"
                )
                rows = cur.fetchall()
    except psycopg.Error:
        return []

    return sorted({row[0] for row in rows if row[0]})


# ── 待處理佇列 ────────────────────────────────────────
# 手機端只把網址存進 queue 表，下載與轉錄一律在本機端執行。


def fetch_queue() -> list[dict]:
    """取得待處理清單。

    佇列是附屬功能，查詢失敗時回傳空列表而非拋例外，
    不讓資料庫連不上擋住本機端列表頁的正常顯示。
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        return []

    try:
        with psycopg.connect(url, connect_timeout=CONNECT_TIMEOUT) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "select id, url, episode_guid, title, note, created_at"
                    " from queue where status = 'pending' order by created_at desc"
                )
                rows = cur.fetchall()
    except psycopg.Error:
        return []

    return [
        {
            "id": str(row[0]),
            "url": row[1],
            "episode_guid": row[2],
            "title": row[3],
            "note": row[4],
            "created_at": _iso(row[5]),
        }
        for row in rows
    ]


def annotate_queue_item(item_id: str, *, episode_guid: str, title: str) -> None:
    """回填本機端解析出的 guid 與標題。

    手機端入列時不解析網址，清單只看得到原始網址；
    本機端一開始處理就補上，之後清單即可顯示集數標題。
    失敗不拋例外：這只影響顯示，不該讓轉錄因此中止。
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        return

    try:
        with psycopg.connect(url, connect_timeout=CONNECT_TIMEOUT) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "update queue set episode_guid = %s, title = %s where id = %s",
                    (episode_guid, title, item_id),
                )
                conn.commit()
    except psycopg.Error:
        return


def resolve_queue_item(episode_guid: str) -> None:
    """把該集對應的待處理項目標記為已完成。

    以 episode_guid 比對而非 id：同一集可能被不同人各貼一次，
    處理完應一併消掉，不留下已無意義的重複項目。
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        return

    try:
        with psycopg.connect(url, connect_timeout=CONNECT_TIMEOUT) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "update queue set status = 'done', processed_at = now()"
                    " where episode_guid = %s and status = 'pending'",
                    (episode_guid,),
                )
                conn.commit()
    except psycopg.Error:
        return


def remove_queue_item(item_id: str) -> None:
    """從佇列移除一筆（使用者決定不處理）。"""
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise UploadError("未設定 DATABASE_URL")

    try:
        with psycopg.connect(url, connect_timeout=CONNECT_TIMEOUT) as conn:
            with conn.cursor() as cur:
                cur.execute("delete from queue where id = %s", (item_id,))
                conn.commit()
    except psycopg.Error as exc:
        raise UploadError(f"移除失敗：{exc}") from exc
