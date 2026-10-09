"""研究專案：使用者自訂的研究主題，把相關的 Podcast、文章、論文歸在一起。

專案只存在資料庫（projects、project_items 兩張表），兩台電腦共用；
尚未上傳的單集也能先歸類，對照表只存 episode_guid、不設外鍵。
與標籤不同：標籤由模型產生，專案由使用者建立與歸類。

專案頁另有研究問題（project_questions）、專案筆記（projects.note）
與 AI 整理結果（project_insights），見 specs_20261009_研究專案頁面.md。
"""
from __future__ import annotations

import os
from datetime import datetime

import psycopg
from psycopg.types.json import Jsonb

CONNECT_TIMEOUT = 20


class ProjectError(Exception):
    """專案操作失敗。訊息需可直接顯示給使用者。"""


class ProjectNotFound(ProjectError):
    """查無專案或研究問題。"""


class ProjectConflict(ProjectError):
    """筆記已被另一台裝置修改，為免覆蓋而拒絕儲存。"""


QUESTION_STATUSES = ("open", "partial", "resolved")


def list_projects() -> list[dict]:
    """列出所有專案與各自的內容數，依名稱排序。

    Raises:
        ProjectError: 未設定 DATABASE_URL 或查詢失敗。
    """
    rows = _query(
        """
        select p.id, p.name, p.description, p.created_at, count(i.episode_guid), max(i.added_at)
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
            "last_added_at": _iso(row[5]) or None,
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


# ── 專案頁 ──────────────────────────────────────────

QUESTION_COLUMNS = "id, text, status, position, note, created_at, updated_at"


def get_project(project_id: str) -> dict:
    """專案頁所需資料：專案、筆記、研究問題（依排序）、AI 整理與各篇的歸入時間。

    同一個連線查完：每次連線 Supabase 約要 0.8 秒，分開連會讓專案頁慢好幾秒。

    Raises:
        ProjectNotFound: 查無此專案。
        ProjectError: 查詢失敗。
    """
    try:
        with psycopg.connect(_url(), connect_timeout=CONNECT_TIMEOUT) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "select id, name, description, note, note_updated_at, created_at"
                    " from projects where id = %s",
                    (project_id,),
                )
                row = cur.fetchone()
                if not row:
                    raise ProjectNotFound("查無此專案")
                cur.execute(
                    f"select {QUESTION_COLUMNS} from project_questions where project_id = %s"
                    " order by position, created_at",
                    (project_id,),
                )
                questions = cur.fetchall()
                cur.execute(
                    "select claims, mindmap, gaps, source_guids, generated_at, suggestions,"
                    " suggested_at, provenance from project_insights where project_id = %s",
                    (project_id,),
                )
                insights = cur.fetchone()
                cur.execute(
                    "select episode_guid, added_at from project_items where project_id = %s"
                    " order by added_at",
                    (project_id,),
                )
                items = cur.fetchall()
    except ProjectError:
        raise
    except psycopg.errors.UndefinedTable as exc:
        raise ProjectError("資料庫還沒有研究專案頁的資料表，請先執行 supabase/schema.sql") from exc
    except (psycopg.errors.InvalidTextRepresentation, psycopg.errors.DataException) as exc:
        raise ProjectError("專案代碼格式錯誤") from exc
    except psycopg.Error as exc:
        raise ProjectError(f"資料庫操作失敗：{exc}") from exc
    return {
        "id": str(row[0]),
        "name": row[1],
        "description": row[2],
        "note": row[3],
        "note_updated_at": _iso(row[4]) or None,
        "created_at": _iso(row[5]),
        "questions": [_question(q) for q in questions],
        "insights": _insights(insights) if insights else None,
        "items": [{"guid": g, "added_at": _iso(t)} for g, t in items],
    }


def project_guids(project_id: str) -> list[str]:
    """專案內所有篇目的 guid（含尚未上傳的），依歸入時間排序。"""
    rows = _query(
        "select episode_guid from project_items where project_id = %s order by added_at",
        (project_id,),
    )
    return [row[0] for row in rows]


def save_note(project_id: str, note: str, base: str) -> str:
    """儲存專案筆記。

    Args:
        base: 使用者開始編輯時載入的筆記內容；資料庫已不同表示另一台裝置改過，拒絕儲存。

    Returns:
        新的 note_updated_at。

    Raises:
        ProjectConflict: 筆記已被另一台裝置修改。
        ProjectNotFound: 查無此專案。
    """
    rows = _query(
        "update projects set note = %s, note_updated_at = now()"
        " where id = %s and note = %s returning note_updated_at",
        (note, project_id, base),
    )
    if rows:
        return _iso(rows[0][0])
    if not _query("select 1 from projects where id = %s", (project_id,)):
        raise ProjectNotFound("查無此專案")
    raise ProjectConflict("另一台裝置已修改這份筆記，請重新載入後再編輯")


def add_questions(project_id: str, texts: list[str], *, from_suggestions: bool = False) -> list[dict]:
    """在清單最後新增研究問題。

    Args:
        from_suggestions: 由 AI 建議勾選加入；同一個交易裡清空其餘建議（spec §8）。

    Raises:
        ProjectNotFound: 查無此專案。
        ProjectError: 問題空白或寫入失敗。
    """
    texts = [t.strip() for t in texts if t and t.strip()]
    if not texts:
        raise ProjectError("請輸入研究問題")
    try:
        with psycopg.connect(_url(), connect_timeout=CONNECT_TIMEOUT) as conn:
            with conn.cursor() as cur:
                cur.execute("select 1 from projects where id = %s for update", (project_id,))
                if not cur.fetchone():
                    raise ProjectNotFound("查無此專案")
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
                if from_suggestions:
                    cur.execute(
                        "update project_insights set suggestions = '[]'::jsonb where project_id = %s",
                        (project_id,),
                    )
    except psycopg.errors.CheckViolation as exc:
        raise ProjectError("研究問題最多 200 字") from exc
    except (psycopg.errors.InvalidTextRepresentation, psycopg.errors.DataException) as exc:
        raise ProjectError("專案代碼格式錯誤") from exc
    except psycopg.Error as exc:
        raise ProjectError(f"新增研究問題失敗：{exc}") from exc
    return created


def update_question(
    project_id: str,
    question_id: str,
    *,
    text: str | None = None,
    status: str | None = None,
    note: str | None = None,
    base_note: str | None = None,
) -> dict:
    """修改研究問題的文字、狀態或筆記；未帶的欄位不變。

    Args:
        base_note: 改筆記時必帶，為開始編輯時載入的內容，用法同 save_note。

    Raises:
        ProjectConflict: 筆記已被另一台裝置修改。
        ProjectNotFound: 查無此研究問題。
        ProjectError: 內容不合規定或寫入失敗。
    """
    sets, params = [], {"id": question_id, "project": project_id}
    where = ["id = %(id)s", "project_id = %(project)s"]
    if text is not None:
        if not text.strip():
            raise ProjectError("請輸入研究問題")
        sets.append("text = %(text)s")
        params["text"] = text.strip()
    if status is not None:
        if status not in QUESTION_STATUSES:
            raise ProjectError("不明的研究問題狀態")
        sets.append("status = %(status)s")
        params["status"] = status
    if note is not None:
        if base_note is None:
            raise ProjectError("缺少編輯前的筆記內容")
        sets.append("note = %(note)s")
        where.append("note = %(base)s")
        params.update(note=note, base=base_note)
    if not sets:
        raise ProjectError("沒有要修改的欄位")
    sets.append("updated_at = now()")
    try:
        rows = _query(
            f"update project_questions set {', '.join(sets)} where {' and '.join(where)}"
            f" returning {QUESTION_COLUMNS}",
            params,
        )
    except psycopg.errors.UniqueViolation:
        raise
    if rows:
        return _question(rows[0])
    if note is not None and _query(
        "select 1 from project_questions where id = %s and project_id = %s", (question_id, project_id)
    ):
        raise ProjectConflict("另一台裝置已修改這則筆記，請重新載入後再編輯")
    raise ProjectNotFound("查無此研究問題")


def delete_question(project_id: str, question_id: str) -> None:
    """刪除研究問題。AI 整理裡這題的缺口會殘留在 gaps，顯示時略過。

    Raises:
        ProjectNotFound: 查無此研究問題。
    """
    if not _query(
        "delete from project_questions where id = %s and project_id = %s returning id",
        (question_id, project_id),
    ):
        raise ProjectNotFound("查無此研究問題")


def reorder_questions(project_id: str, question_ids: list[str]) -> None:
    """依 question_ids 的順序重寫排序位置；清單須與專案現有問題完全一致。

    Raises:
        ProjectError: 清單與現有問題不符（另一台裝置剛新增或刪除），或寫入失敗。
    """
    try:
        with psycopg.connect(_url(), connect_timeout=CONNECT_TIMEOUT) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "select id from project_questions where project_id = %s for update",
                    (project_id,),
                )
                existing = {str(row[0]) for row in cur.fetchall()}
                if existing != set(question_ids) or len(question_ids) != len(existing):
                    raise ProjectConflict("研究問題已在另一台裝置變動，請重新載入")
                for position, question_id in enumerate(question_ids):
                    cur.execute(
                        "update project_questions set position = %s where id = %s",
                        (position, question_id),
                    )
    except ProjectError:
        raise
    except (psycopg.errors.InvalidTextRepresentation, psycopg.errors.DataException) as exc:
        raise ProjectError("代碼格式錯誤") from exc
    except psycopg.Error as exc:
        raise ProjectError(f"調整順序失敗：{exc}") from exc


def clear_suggestions(project_id: str) -> None:
    """清空尚未處理的建議問題（「都不要」）。"""
    _query("update project_insights set suggestions = '[]'::jsonb where project_id = %s", (project_id,))


def save_insights(
    project_id: str,
    *,
    claims: dict,
    mindmap: str,
    gaps: dict,
    source_guids: list[str],
    provenance: dict,
) -> None:
    """寫入對照表、心智圖與缺口；整組覆蓋，建議問題不動。"""
    _query(
        """
        insert into project_insights
          (project_id, claims, mindmap, gaps, source_guids, generated_at, provenance)
        values (%(id)s, %(claims)s, %(mindmap)s, %(gaps)s, %(guids)s, now(), %(prov)s)
        on conflict (project_id) do update set
          claims = excluded.claims, mindmap = excluded.mindmap, gaps = excluded.gaps,
          source_guids = excluded.source_guids, generated_at = excluded.generated_at,
          provenance = project_insights.provenance || excluded.provenance
        returning project_id
        """,
        {
            "id": project_id,
            "claims": Jsonb(claims),
            "mindmap": mindmap,
            "gaps": Jsonb(gaps),
            "guids": source_guids,
            "prov": Jsonb({"insights": provenance}),
        },
    )


def save_suggestions(project_id: str, suggestions: list[dict], provenance: dict) -> None:
    """寫入建議問題，取代尚未處理的那批。"""
    _query(
        """
        insert into project_insights (project_id, suggestions, suggested_at, provenance)
        values (%(id)s, %(sugg)s, now(), %(prov)s)
        on conflict (project_id) do update set
          suggestions = excluded.suggestions, suggested_at = excluded.suggested_at,
          provenance = project_insights.provenance || excluded.provenance
        returning project_id
        """,
        {"id": project_id, "sugg": Jsonb(suggestions), "prov": Jsonb({"suggestions": provenance})},
    )


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


def _insights(row: tuple) -> dict:
    return {
        "claims": row[0],
        "mindmap": row[1],
        "gaps": row[2] or {},
        "source_guids": list(row[3] or []),
        "generated_at": _iso(row[4]) or None,
        "suggestions": row[5] or [],
        "suggested_at": _iso(row[6]) or None,
        "provenance": row[7] or {},
    }


def add_episode_to_projects(episode_guid: str, project_ids: list[str]) -> None:
    """把這集加入指定專案，保留原有的歸類；已被刪除的專案略過。

    Raises:
        ProjectError: 寫入失敗。
    """
    for project_id in dict.fromkeys(project_ids):
        _query(
            "insert into project_items (project_id, episode_guid)"
            " select id, %s from projects where id = %s"
            " on conflict do nothing returning project_id",
            (episode_guid, project_id),
        )


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
    except psycopg.errors.CheckViolation as exc:
        raise ProjectError("內容超過長度上限") from exc
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
