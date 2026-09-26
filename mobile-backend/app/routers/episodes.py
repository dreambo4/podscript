from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from ..database import get_connection
from ..dependencies import get_current_user

router = APIRouter(tags=["集數"], dependencies=[Depends(get_current_user)])

TAG_MODE_OPERATORS = {
    "all": "@>",  # AND：hashtags 包含所選全部標籤
    "any": "&&",  # OR：hashtags 與所選標籤有交集
}

LIST_COLUMNS = """
    e.id, e.episode_guid, e.podcast_name, e.title, e.published_at, e.created_at,
    e.duration_sec, e.hashtags, (f.episode_id is not null) as is_favorite
"""

DETAIL_COLUMNS = """
    e.id, e.episode_guid, e.podcast_name, e.title, e.published_at, e.duration_sec,
    e.summary, e.mindmap_mermaid, e.hashtags, e.transcript, e.speakers, e.provenance,
    (f.episode_id is not null) as is_favorite, e.platform, e.source_url
"""

FAVORITE_JOIN = "left join favorites f on f.episode_id = e.id and f.user_id = %s"

SORT_COLUMNS = {
    "created_at": "created_at",
    "published_at": "published_at",
}


def _row_to_summary(row: tuple) -> dict:
    return {
        "id": str(row[0]),
        "episode_guid": row[1],
        "podcast_name": row[2],
        "title": row[3],
        "published_at": row[4].isoformat() if row[4] else None,
        "created_at": row[5].isoformat() if row[5] else None,
        "duration_sec": row[6],
        "hashtags": row[7] or [],
        "is_favorite": row[8],
    }


@router.get("/episodes", summary="搜尋／瀏覽集數列表")
def list_episodes(
    q: str | None = Query(None, description="搜尋標題或逐字稿內文"),
    tag: str | None = Query(None, description="依單一標籤篩選（舊參數，等同 tags 傳一個值）"),
    tags: list[str] | None = Query(None, description="依多個標籤篩選，搭配 tag_mode"),
    tag_mode: Literal["all", "any"] = Query("all", description="all=須同時包含全部標籤，any=符合任一標籤"),
    channel: str | None = Query(None, description="依頻道（podcast_name）篩選"),
    favorites_only: bool = Query(False, description="只列出目前使用者收藏的集數"),
    sort: Literal["created_at", "published_at"] = Query("created_at", description="排序基準"),
    limit: int = Query(20, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
) -> list[dict]:
    where = []
    params: list = [user["id"]]  # FAVORITE_JOIN 的 %s

    if q:
        # pg_trgm 模糊比對：標題或攤平純文字命中皆算
        where.append("(e.title ilike %s or e.transcript_text ilike %s)")
        params.extend([f"%{q}%", f"%{q}%"])

    all_tags = list(tags or [])
    if tag:
        all_tags.append(tag)
    if all_tags:
        where.append(f"e.hashtags {TAG_MODE_OPERATORS[tag_mode]} %s")
        params.append(all_tags)

    if channel:
        where.append("e.podcast_name = %s")
        params.append(channel)

    if favorites_only:
        where.append("f.episode_id is not null")

    where_sql = f"where {' and '.join(where)}" if where else ""
    sort_column = SORT_COLUMNS[sort]

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"select {LIST_COLUMNS} from episodes e {FAVORITE_JOIN} {where_sql}"
                f" order by e.{sort_column} desc nulls last limit %s offset %s",
                (*params, limit, offset),
            )
            rows = cur.fetchall()

    return [_row_to_summary(row) for row in rows]


@router.get("/tags", summary="標籤清單（依集數多寡排序）")
def list_tags() -> list[dict]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "select tag, count(*) from episodes, unnest(hashtags) as tag"
                " group by tag order by count(*) desc, tag"
            )
            rows = cur.fetchall()

    return [{"tag": row[0], "count": row[1]} for row in rows]


@router.get("/channels", summary="頻道清單（依集數多寡排序）")
def list_channels() -> list[dict]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "select podcast_name, count(*) from episodes"
                " group by podcast_name order by count(*) desc, podcast_name"
            )
            rows = cur.fetchall()

    return [{"podcast_name": row[0], "count": row[1]} for row in rows]


@router.get("/episodes/{guid}", summary="單集詳細內容")
def get_episode(guid: str, user: dict = Depends(get_current_user)) -> dict:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"select {DETAIL_COLUMNS} from episodes e {FAVORITE_JOIN} where e.episode_guid = %s",
                (user["id"], guid),
            )
            row = cur.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="集數不存在")

    return {
        "id": str(row[0]),
        "episode_guid": row[1],
        "podcast_name": row[2],
        "title": row[3],
        "published_at": row[4].isoformat() if row[4] else None,
        "duration_sec": row[5],
        "summary": row[6],
        "mindmap_mermaid": row[7],
        "hashtags": row[8] or [],
        "transcript": row[9],
        "speakers": row[10] or {},
        "provenance": row[11] or {},
        "is_favorite": row[12],
        "platform": row[13],
        "source_url": row[14],
    }


@router.put("/episodes/{guid}/favorite", status_code=204, summary="收藏此集")
def add_favorite(guid: str, user: dict = Depends(get_current_user)) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("select id from episodes where episode_guid = %s", (guid,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="集數不存在")

            cur.execute(
                "insert into favorites (user_id, episode_id) values (%s, %s)"
                " on conflict (user_id, episode_id) do nothing",
                (user["id"], row[0]),
            )
            conn.commit()


@router.delete("/episodes/{guid}/favorite", status_code=204, summary="取消收藏此集")
def remove_favorite(guid: str, user: dict = Depends(get_current_user)) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "delete from favorites where user_id = %s"
                " and episode_id = (select id from episodes where episode_guid = %s)",
                (user["id"], guid),
            )
            conn.commit()
