"""研究專案：使用者自訂的研究主題，把相關的 Podcast、文章、論文歸在一起。

專案只存在資料庫（projects、project_items 兩張表），兩台電腦共用；
尚未上傳的單集也能先歸類，對照表只存 episode_guid、不設外鍵。
與標籤不同：標籤由模型產生，專案由使用者建立與歸類。
"""
from __future__ import annotations

import os
from datetime import datetime

import psycopg

CONNECT_TIMEOUT = 20


class ProjectError(Exception):
    """專案操作失敗。訊息需可直接顯示給使用者。"""


def list_projects() -> list[dict]:
    """列出所有專案與各自的內容數，依名稱排序。

    Raises:
        ProjectError: 未設定 DATABASE_URL 或查詢失敗。
    """
    rows = _query(
        """
        select p.id, p.name, p.description, p.created_at, count(i.episode_guid)
        from projects p left join project_items i on i.project_id = p.id
        group by p.id order by p.name
        """
    )
    return [
        {
            "id": str(row[0]),
            "name": row[1],
            "description": row[2],
            "created_at": _iso(row[3]),
            "item_count": row[4],
        }
        for row in rows
    ]


def create_project(name: str, description: str = "") -> dict:
    """建立專案。

    Raises:
        ProjectError: 名稱空白、重複，或寫入失敗。
    """
    name = name.strip()
    if not name:
        raise ProjectError("請輸入專案名稱")
    try:
        rows = _query(
            "insert into projects (name, description) values (%s, %s)"
            " returning id, name, description, created_at",
            (name, description.strip()),
        )
    except psycopg.errors.UniqueViolation as exc:
        raise ProjectError(f"已有名為「{name}」的專案") from exc
    row = rows[0]
    return {
        "id": str(row[0]),
        "name": row[1],
        "description": row[2],
        "created_at": _iso(row[3]),
        "item_count": 0,
    }


def update_project(project_id: str, *, name: str | None = None, description: str | None = None) -> None:
    """修改專案名稱或說明。

    Raises:
        ProjectError: 名稱空白或重複、查無此專案，或寫入失敗。
    """
    sets, params = [], {"id": project_id}
    if name is not None:
        if not name.strip():
            raise ProjectError("請輸入專案名稱")
        sets.append("name = %(name)s")
        params["name"] = name.strip()
    if description is not None:
        sets.append("description = %(description)s")
        params["description"] = description.strip()
    if not sets:
        return
    sets.append("updated_at = now()")
    try:
        rows = _query(
            f"update projects set {', '.join(sets)} where id = %(id)s returning id", params
        )
    except psycopg.errors.UniqueViolation as exc:
        raise ProjectError(f"已有名為「{name}」的專案") from exc
    if not rows:
        raise ProjectError("查無此專案")


def delete_project(project_id: str) -> None:
    """刪除專案；歸類關係連帶刪除，單集本身不受影響。

    Raises:
        ProjectError: 查無此專案或刪除失敗。
    """
    if not _query("delete from projects where id = %s returning id", (project_id,)):
        raise ProjectError("查無此專案")


def projects_of(episode_guid: str) -> list[str]:
    """這集所屬的專案 id。查詢失敗時回傳空列表，不讓單集頁因此載入失敗。"""
    try:
        rows = _query(
            "select project_id from project_items where episode_guid = %s", (episode_guid,)
        )
    except ProjectError:
        return []
    return [str(row[0]) for row in rows]


def projects_by_episode() -> dict[str, list[str]]:
    """所有單集所屬的專案 id，供清單篩選。查詢失敗時回傳空 dict。"""
    try:
        rows = _query("select episode_guid, project_id from project_items")
    except ProjectError:
        return {}
    result: dict[str, list[str]] = {}
    for guid, project_id in rows:
        result.setdefault(guid, []).append(str(project_id))
    return result


def set_episode_projects(episode_guid: str, project_ids: list[str]) -> list[str]:
    """把這集的所屬專案設為 project_ids（不在其中的移除，新的加入）。

    Raises:
        ProjectError: 專案不存在或寫入失敗。
    """
    url = _url()
    wanted = sorted(set(project_ids))
    try:
        with psycopg.connect(url, connect_timeout=CONNECT_TIMEOUT) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "delete from project_items where episode_guid = %s"
                    " and not (project_id = any(%s::uuid[]))",
                    (episode_guid, wanted),
                )
                for project_id in wanted:
                    cur.execute(
                        "insert into project_items (project_id, episode_guid) values (%s, %s)"
                        " on conflict do nothing",
                        (project_id, episode_guid),
                    )
    except psycopg.errors.ForeignKeyViolation as exc:
        raise ProjectError("有專案已被刪除，請重新整理") from exc
    except (psycopg.errors.InvalidTextRepresentation, psycopg.errors.DataException) as exc:
        raise ProjectError("專案代碼格式錯誤") from exc
    except psycopg.Error as exc:
        raise ProjectError(f"更新專案失敗：{exc}") from exc
    return wanted


def remove_episode(episode_guid: str) -> None:
    """刪除單集時一併移除它的歸類。失敗不拋例外：殘留的對照列不影響任何顯示。"""
    try:
        _query(
            "delete from project_items where episode_guid = %s returning project_id",
            (episode_guid,),
        )
    except ProjectError:
        return


def _query(sql: str, params=None) -> list[tuple]:
    try:
        with psycopg.connect(_url(), connect_timeout=CONNECT_TIMEOUT) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall() if cur.description else []
    except (psycopg.errors.UniqueViolation, psycopg.errors.ForeignKeyViolation):
        raise
    except psycopg.errors.UndefinedTable as exc:
        raise ProjectError("資料庫還沒有專案資料表，請先執行 supabase/schema.sql 的研究專案區段") from exc
    except (psycopg.errors.InvalidTextRepresentation, psycopg.errors.DataException) as exc:
        raise ProjectError("專案代碼格式錯誤") from exc
    except psycopg.Error as exc:
        raise ProjectError(f"資料庫操作失敗：{exc}") from exc


def _url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise ProjectError("未設定 DATABASE_URL，無法使用研究專案")
    return url


def _iso(value) -> str:
    return value.isoformat() if isinstance(value, datetime) else str(value or "")
