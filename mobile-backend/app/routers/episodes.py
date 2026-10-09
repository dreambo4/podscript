import re
from typing import Literal
from uuid import UUID

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
    e.duration_sec, e.hashtags, (f.episode_id is not null) as is_favorite, e.platform, e.cover
"""

DETAIL_COLUMNS = """
    e.id, e.episode_guid, e.podcast_name, e.title, e.published_at, e.duration_sec,
    e.summary, e.mindmap_mermaid, e.hashtags, e.transcript, e.speakers, e.provenance,
    (f.episode_id is not null) as is_favorite, e.platform, e.source_url, e.chapters, e.cover
"""

FAVORITE_JOIN = "left join favorites f on f.episode_id = e.id and f.user_id = %s"

# 搜尋命中片段：取第一個命中處前後各幾個字
SNIPPET_RADIUS = 40

# 有 q 時才加入：在 SQL 裡算出第一個命中位置與次數，避免把整份逐字稿拉回 Python。
# 樣式與旗標都以參數（%s）傳入，使用者輸入不會拼進 SQL 字串。
MATCH_JOIN = """
    cross join lateral (
        select coalesce(e.transcript_text, '') as txt, %s::text as pat, %s::text as flags
    ) t
    cross join lateral (
        select regexp_instr(t.txt, t.pat, 1, 1, 0, t.flags) as pos,
               regexp_instr(t.txt, t.pat, 1, 1, 1, t.flags) as pos_end,
               regexp_count(t.txt, t.pat, 1, t.flags) as cnt
    ) m
"""

MATCH_COLUMNS = f"""
    , case when m.pos > 0 then substr(
        e.transcript_text,
        greatest(m.pos - {SNIPPET_RADIUS}, 1),
        least(m.pos - 1, {SNIPPET_RADIUS}) + (m.pos_end - m.pos) + {SNIPPET_RADIUS}
    ) end as snippet,
    m.pos > {SNIPPET_RADIUS} + 1 as cut_start,
    m.pos > 0 and m.pos_end - 1 + {SNIPPET_RADIUS} < length(t.txt) as cut_end,
    m.cnt as match_count
"""

# 全字相符的「字」只算英數與底線：中文與英文常直接相連（如「我的AI我」），
# 若沿用正規表示式的 \y，中文字也算字元，AI 就永遠不會被視為獨立的字。
# 只在關鍵字頭（尾）是英數時才檢查前（後）一字，中文關鍵字開不開全字結果相同。
WORD_CHAR = re.compile(r"[A-Za-z0-9_]")
NOT_WORD_BEFORE = "(?<![A-Za-z0-9_])"
NOT_WORD_AFTER = "(?![A-Za-z0-9_])"

# 類型篩選：YouTube 為影片、文章與論文各自一類，其餘平台皆視為音檔
KIND_CONDITIONS = {
    "audio": "coalesce(e.platform, '') not in ('youtube', 'article', 'paper')",
    "video": "e.platform = 'youtube'",
    "article": "e.platform = 'article'",
    "paper": "e.platform = 'paper'",
}

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
        "platform": row[9],
        "cover": row[10],
        "snippet": None,
        "match_count": 0,
    }


def _row_to_match(row: tuple) -> dict:
    """LIST_COLUMNS 之後接 MATCH_COLUMNS 的列；只命中標題時 snippet 為 None。"""
    item = _row_to_summary(row)
    snippet, cut_start, cut_end, match_count = row[11:15]
    if snippet:
        item["snippet"] = ("…" if cut_start else "") + snippet + ("…" if cut_end else "")
    item["match_count"] = match_count
    return item


def _regex_pattern(q: str, whole_word: bool) -> str:
    """關鍵字轉成只比對字面的正規表示式。

    ASCII 標點與空白一律加反斜線（ARE 中反斜線接非英數字元即該字元本身），
    英數與中文不需跳脫；因此樣式只有固定的邊界條件，不會有回溯爆炸。
    """
    literal = "".join(
        "\\" + ch if ch.isascii() and not ch.isalnum() else ch for ch in q
    )
    if not whole_word:
        return literal
    before = NOT_WORD_BEFORE if WORD_CHAR.fullmatch(q[0]) else ""
    after = NOT_WORD_AFTER if WORD_CHAR.fullmatch(q[-1]) else ""
    return before + literal + after


def _like_pattern(q: str) -> str:
    """ilike 的 % 與 _ 是萬用字元；跳脫後與命中片段的字面比對一致。"""
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


@router.get("/episodes", summary="搜尋／瀏覽集數列表")
def list_episodes(
    q: str | None = Query(None, description="搜尋標題或逐字稿內文"),
    tag: str | None = Query(None, description="依單一標籤篩選（舊參數，等同 tags 傳一個值）"),
    tags: list[str] | None = Query(None, description="依多個標籤篩選，搭配 tag_mode"),
    tag_mode: Literal["all", "any"] = Query("all", description="all=須同時包含全部標籤，any=符合任一標籤"),
    channel: str | None = Query(None, description="依頻道（podcast_name）篩選"),
    kind: Literal["audio", "video", "article", "paper"] | None = Query(
        None,
        description="依類型篩選：audio=音檔（YouTube、文章、論文以外）、video=影片（YouTube）、article=文章、paper=論文",
    ),
    case_sensitive: bool = Query(False, description="搜尋時大小寫須相符"),
    whole_word: bool = Query(False, description="搜尋時全字拼寫須相符（字只算英數與底線）"),
    favorites_only: bool = Query(False, description="只列出目前使用者收藏的集數"),
    project: UUID | None = Query(None, description="只列出屬於此研究專案（projects.id）的集數"),
    sort: Literal["created_at", "published_at"] = Query("created_at", description="排序基準"),
    limit: int = Query(20, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
) -> list[dict]:
    where = []
    params: list = [user["id"]]  # FAVORITE_JOIN 的 %s

    if q:
        pattern = _regex_pattern(q, whole_word)
        params.extend([pattern, "" if case_sensitive else "i"])  # MATCH_JOIN 的兩個 %s
        # pg_trgm 模糊比對：標題或攤平純文字命中皆算；like／ilike 可走 trgm 索引先篩一輪
        like = "like" if case_sensitive else "ilike"
        where.append(f"(e.title {like} %s or e.transcript_text {like} %s)")
        params.extend([_like_pattern(q), _like_pattern(q)])
        if whole_word:
            # like 只能篩出含該字串者，全字與否再以正規表示式確認
            regex = "~" if case_sensitive else "~*"
            where.append(f"(e.title {regex} %s or m.pos > 0)")
            params.append(pattern)

    all_tags = list(tags or [])
    if tag:
        all_tags.append(tag)
    if all_tags:
        where.append(f"e.hashtags {TAG_MODE_OPERATORS[tag_mode]} %s")
        params.append(all_tags)

    if channel:
        where.append("e.podcast_name = %s")
        params.append(channel)

    if kind:
        where.append(KIND_CONDITIONS[kind])

    if favorites_only:
        where.append("f.episode_id is not null")

    if project:
        where.append(
            "e.episode_guid in (select episode_guid from project_items where project_id = %s)"
        )
        params.append(project)

    where_sql = f"where {' and '.join(where)}" if where else ""
    sort_column = SORT_COLUMNS[sort]

    columns = LIST_COLUMNS + (MATCH_COLUMNS if q else "")
    match_join = MATCH_JOIN if q else ""

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"select {columns} from episodes e {FAVORITE_JOIN} {match_join} {where_sql}"
                f" order by e.{sort_column} desc nulls last limit %s offset %s",
                (*params, limit, offset),
            )
            rows = cur.fetchall()

    return [_row_to_match(row) if q else _row_to_summary(row) for row in rows]


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
            if row:
                cur.execute(
                    "select p.id, p.name from projects p"
                    " join project_items i on i.project_id = p.id"
                    " where i.episode_guid = %s order by p.name",
                    (guid,),
                )
                projects = [{"id": str(r[0]), "name": r[1]} for r in cur.fetchall()]

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
        "chapters": row[15] or [],
        "cover": row[16],
        "projects": projects,
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
