"""研究專案：使用者自訂的研究主題，把相關的 Podcast、文章、論文歸在一起。

本機與手機都能管理專案與歸類；手機只看得到已上傳的內容，
本機尚未上傳的單集也能歸類（project_items 只存 episode_guid，不設外鍵）。
"""
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..database import get_connection
from ..dependencies import get_current_user

router = APIRouter(tags=["研究專案"], dependencies=[Depends(get_current_user)])


class ProjectBody(BaseModel):
    name: str | None = Field(None, max_length=100)
    description: str | None = Field(None, max_length=2000)


class EpisodeProjectsBody(BaseModel):
    project_ids: list[UUID]


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
