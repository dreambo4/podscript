"""研究專案：使用者自訂的研究主題，把相關的 Podcast、文章、論文歸在一起。

本機與手機都能管理專案與歸類；手機只看得到已上傳的內容，
本機尚未上傳的單集也能歸類（project_items 只存 episode_guid，不設外鍵）。

專案頁：研究問題與筆記可在手機編輯；AI 整理只在本機產生，手機只讀。
篇目列表與專案內搜尋沿用 GET /episodes?project=。
"""
from typing import Literal
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..database import get_connection
from ..dependencies import get_current_user
from .episodes import FAVORITE_JOIN, LIST_COLUMNS, _row_to_summary

router = APIRouter(tags=["研究專案"], dependencies=[Depends(get_current_user)])


class ProjectBody(BaseModel):
    name: str | None = Field(None, max_length=100)
    description: str | None = Field(None, max_length=2000)


class EpisodeProjectsBody(BaseModel):
    project_ids: list[UUID]


class NoteBody(BaseModel):
    note: str = Field(..., max_length=50000)
    base: str  # 開始編輯時載入的內容；資料庫已不同表示另一台裝置改過


class QuestionsBody(BaseModel):
    texts: list[str] = Field(..., min_length=1, max_length=20)
    from_suggestions: bool = False  # 由 AI 建議勾選加入時，其餘建議一併清空


class QuestionBody(BaseModel):
    text: str | None = Field(None, max_length=200)
    status: Literal["open", "partial", "resolved"] | None = None
    note: str | None = Field(None, max_length=20000)
    base_note: str | None = None  # 改筆記時必帶，用法同 NoteBody.base


class QuestionOrderBody(BaseModel):
    ids: list[UUID]


QUESTION_COLUMNS = "id, text, status, position, note, created_at, updated_at"

CONFLICT = "另一台裝置已修改這份筆記，請重新載入後再編輯"


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _question(row: tuple) -> dict:
    return {
        "id": str(row[0]),
        "text": row[1],
        "status": row[2],
        "position": row[3],
        "note": row[4],
        "created_at": _iso(row[5]),
        "updated_at": _iso(row[6]),
    }


@router.get("/projects", summary="研究專案清單（依名稱排序）")
def list_projects() -> list[dict]:
    """count 只算已上傳（episodes 中有）的集數；本機尚未上傳的歸類不計。"""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "select p.id, p.name, p.description, count(e.id)"
                " from projects p"
                " left join project_items i on i.project_id = p.id"
                " left join episodes e on e.episode_guid = i.episode_guid"
                " group by p.id order by p.name"
            )
            rows = cur.fetchall()

    return [
        {"id": str(row[0]), "name": row[1], "description": row[2], "count": row[3]}
        for row in rows
    ]


@router.post("/projects", summary="新增研究專案")
def create_project(body: ProjectBody) -> dict:
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="請輸入專案名稱")
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "insert into projects (name, description) values (%s, %s) returning id",
                    (name, (body.description or "").strip()),
                )
                project_id = cur.fetchone()[0]
                conn.commit()
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(status_code=400, detail=f"已有名為「{name}」的專案") from exc
    return {"id": str(project_id), "name": name, "description": (body.description or "").strip(), "count": 0}


@router.put("/projects/{project_id}", status_code=204, summary="修改研究專案名稱或說明")
def update_project(project_id: UUID, body: ProjectBody) -> None:
    sets, params = [], {"id": project_id}
    if body.name is not None:
        if not body.name.strip():
            raise HTTPException(status_code=400, detail="請輸入專案名稱")
        sets.append("name = %(name)s")
        params["name"] = body.name.strip()
    if body.description is not None:
        sets.append("description = %(description)s")
        params["description"] = body.description.strip()
    if not sets:
        return
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"update projects set {', '.join(sets)}, updated_at = now()"
                    " where id = %(id)s returning id",
                    params,
                )
                found = cur.fetchone()
                conn.commit()
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(status_code=400, detail=f"已有名為「{body.name.strip()}」的專案") from exc
    if not found:
        raise HTTPException(status_code=404, detail="查無此專案")


@router.delete("/projects/{project_id}", status_code=204, summary="刪除研究專案（單集不受影響）")
def delete_project(project_id: UUID) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("delete from projects where id = %s returning id", (project_id,))
            found = cur.fetchone()
            conn.commit()
    if not found:
        raise HTTPException(status_code=404, detail="查無此專案")


@router.put("/episodes/{guid}/projects", summary="設定單集所屬的研究專案（整組取代）")
def set_episode_projects(guid: str, body: EpisodeProjectsBody) -> dict:
    wanted = sorted({str(p) for p in body.project_ids})
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("select 1 from episodes where episode_guid = %s", (guid,))
                if not cur.fetchone():
                    raise HTTPException(status_code=404, detail="集數不存在")
                cur.execute(
                    "delete from project_items where episode_guid = %s"
                    " and not (project_id = any(%s::uuid[]))",
                    (guid, wanted),
                )
                for project_id in wanted:
                    cur.execute(
                        "insert into project_items (project_id, episode_guid) values (%s, %s)"
                        " on conflict do nothing",
                        (project_id, guid),
                    )
                conn.commit()
    except psycopg.errors.ForeignKeyViolation as exc:
        raise HTTPException(status_code=400, detail="有專案已被刪除，請重新整理") from exc
    return {"projects": wanted}


# ── 專案頁 ──────────────────────────────────────────


@router.get("/projects/{project_id}", summary="專案頁資料（專案、筆記、研究問題、AI 整理）")
def get_project(project_id: UUID) -> dict:
    """篇目另以 GET /episodes?project= 取得。AI 整理的篇目可能含本機尚未上傳的集數。"""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "select id, name, description, note, note_updated_at, created_at from projects where id = %s",
                (project_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="查無此專案")
            cur.execute(
                f"select {QUESTION_COLUMNS} from project_questions where project_id = %s"
                " order by position, created_at",
                (project_id,),
            )
            questions = [_question(q) for q in cur.fetchall()]
            cur.execute(
                "select claims, mindmap, gaps, source_guids, generated_at, suggestions, suggested_at"
                " from project_insights where project_id = %s",
                (project_id,),
            )
            ins = cur.fetchone()
            # 只列已上傳的集數；歸入時間供「依歸入時間」排序
            cur.execute(
                "select i.episode_guid, i.added_at from project_items i"
                " join episodes e on e.episode_guid = i.episode_guid"
                " where i.project_id = %s order by i.added_at",
                (project_id,),
            )
            items = [{"guid": r[0], "added_at": _iso(r[1])} for r in cur.fetchall()]

    return {
        "id": str(row[0]),
        "name": row[1],
        "description": row[2],
        "note": row[3],
        "note_updated_at": _iso(row[4]),
        "created_at": _iso(row[5]),
        "items": items,
        "questions": questions,
        "insights": {
            "claims": ins[0],
            "mindmap": ins[1],
            "gaps": ins[2] or {},
            "source_guids": list(ins[3] or []),
            "generated_at": _iso(ins[4]),
            "suggestions": ins[5] or [],
            "suggested_at": _iso(ins[6]),
        }
        if ins
        else None,
    }


@router.put("/projects/{project_id}/note", summary="儲存專案筆記（另一台裝置已修改時回 409）")
def save_note(project_id: UUID, body: NoteBody) -> dict:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "update projects set note = %s, note_updated_at = now()"
                " where id = %s and note = %s returning note_updated_at",
                (body.note, project_id, body.base),
            )
            row = cur.fetchone()
            if not row:
                cur.execute("select 1 from projects where id = %s", (project_id,))
                exists = cur.fetchone()
            conn.commit()
    if row:
        return {"note_updated_at": _iso(row[0])}
    if not exists:
        raise HTTPException(status_code=404, detail="查無此專案")
    raise HTTPException(status_code=409, detail=CONFLICT)


@router.post("/projects/{project_id}/questions", summary="新增研究問題（可一次多筆）")
def add_questions(project_id: UUID, body: QuestionsBody) -> list[dict]:
    texts = [t.strip() for t in body.texts if t.strip()]
    if not texts:
        raise HTTPException(status_code=400, detail="請輸入研究問題")
    if any(len(t) > 200 for t in texts):
        raise HTTPException(status_code=400, detail="研究問題最多 200 字")
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("select 1 from projects where id = %s for update", (project_id,))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="查無此專案")
            cur.execute(
                "select coalesce(max(position), -1) from project_questions where project_id = %s",
                (project_id,),
            )
            position = cur.fetchone()[0]
            created = []
            for text in texts:
                position += 1
                cur.execute(
                    "insert into project_questions (project_id, text, position) values (%s, %s, %s)"
                    f" returning {QUESTION_COLUMNS}",
                    (project_id, text, position),
                )
                created.append(_question(cur.fetchone()))
            if body.from_suggestions:
                cur.execute(
                    "update project_insights set suggestions = '[]'::jsonb where project_id = %s",
                    (project_id,),
                )
            conn.commit()
    return created


# 須在 /questions/{question_id} 之前註冊，否則 order 會被當成問題 id
@router.put("/projects/{project_id}/questions/order", summary="調整研究問題順序（送出整組 id）")
def reorder_questions(project_id: UUID, body: QuestionOrderBody) -> dict:
    wanted = [str(i) for i in body.ids]
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "select id from project_questions where project_id = %s for update", (project_id,)
            )
            existing = {str(r[0]) for r in cur.fetchall()}
            if existing != set(wanted) or len(wanted) != len(existing):
                raise HTTPException(status_code=409, detail="研究問題已在另一台裝置變動，請重新載入")
            for position, question_id in enumerate(wanted):
                cur.execute(
                    "update project_questions set position = %s where id = %s",
                    (position, question_id),
                )
            conn.commit()
    return {"ids": wanted}


@router.put("/projects/{project_id}/questions/{question_id}", summary="修改研究問題的文字、狀態或筆記")
def update_question(project_id: UUID, question_id: UUID, body: QuestionBody) -> dict:
    sets, params = [], {"id": question_id, "project": project_id}
    where = ["id = %(id)s", "project_id = %(project)s"]
    if body.text is not None:
        if not body.text.strip():
            raise HTTPException(status_code=400, detail="請輸入研究問題")
        sets.append("text = %(text)s")
        params["text"] = body.text.strip()
    if body.status is not None:
        sets.append("status = %(status)s")
        params["status"] = body.status
    if body.note is not None:
        if body.base_note is None:
            raise HTTPException(status_code=400, detail="缺少編輯前的筆記內容")
        sets.append("note = %(note)s")
        where.append("note = %(base)s")
        params.update(note=body.note, base=body.base_note)
    if not sets:
        raise HTTPException(status_code=400, detail="沒有要修改的欄位")
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"update project_questions set {', '.join(sets)}, updated_at = now()"
                f" where {' and '.join(where)} returning {QUESTION_COLUMNS}",
                params,
            )
            row = cur.fetchone()
            if not row:
                cur.execute(
                    "select 1 from project_questions where id = %s and project_id = %s",
                    (question_id, project_id),
                )
                exists = cur.fetchone()
            conn.commit()
    if row:
        return _question(row)
    if exists and body.note is not None:
        raise HTTPException(status_code=409, detail=CONFLICT)
    raise HTTPException(status_code=404, detail="查無此研究問題")


@router.delete("/projects/{project_id}/questions/{question_id}", status_code=204, summary="刪除研究問題")
def delete_question(project_id: UUID, question_id: UUID) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "delete from project_questions where id = %s and project_id = %s returning id",
                (question_id, project_id),
            )
            found = cur.fetchone()
            conn.commit()
    if not found:
        raise HTTPException(status_code=404, detail="查無此研究問題")


@router.delete("/projects/{project_id}/suggestions", status_code=204, summary="清空尚未處理的建議問題")
def clear_suggestions(project_id: UUID) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "update project_insights set suggestions = '[]'::jsonb where project_id = %s",
                (project_id,),
            )
            conn.commit()


CANDIDATE_LIMIT = 10


@router.get("/projects/{project_id}/candidates", summary="可能相關：標籤與專案內容重疊、尚未歸入的集數")
def list_candidates(project_id: UUID, user: dict = Depends(get_current_user)) -> list[dict]:
    """依共同標籤數排序，最多 10 筆。

    回傳欄位同集數列表，另加 shared_tags（共同標籤）與 project_ids（目前所屬的專案，
    歸入時整組送回 PUT /episodes/{guid}/projects）。
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                with mine as (
                    select episode_guid from project_items where project_id = %s
                ), tags as (
                    select array_agg(distinct t) as arr
                    from episodes e2 join mine using (episode_guid), unnest(e2.hashtags) t
                )
                select {LIST_COLUMNS},
                       array(select unnest(e.hashtags) intersect select unnest(tags.arr)) as shared,
                       array(select i.project_id::text from project_items i
                             where i.episode_guid = e.episode_guid) as project_ids
                from episodes e {FAVORITE_JOIN}, tags
                where tags.arr is not null and e.hashtags && tags.arr
                  and e.episode_guid not in (select episode_guid from mine)
                order by cardinality(array(select unnest(e.hashtags) intersect select unnest(tags.arr))) desc,
                         e.published_at desc nulls last
                limit %s
                """,
                (project_id, user["id"], CANDIDATE_LIMIT),
            )
            rows = cur.fetchall()
    return [
        _row_to_summary(row) | {"shared_tags": list(row[12] or []), "project_ids": list(row[13] or [])}
        for row in rows
    ]
