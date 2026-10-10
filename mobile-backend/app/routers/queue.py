"""待處理佇列：手機端貼上網址暫存，回家在本機端處理。

手機端不解析網址，只檢查是否為合法的 http(s) 網址；支援哪些平台由本機端 resolver 判斷。
節目名稱與單集標題由本機端解析後回填，因此入列時只有 url。
"""
from urllib.parse import urlparse
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..database import get_connection
from ..dependencies import get_current_user

router = APIRouter(tags=["待處理"], dependencies=[Depends(get_current_user)])

# 只允許 http/https：javascript:、data: 等 scheme 進了資料庫後，
# 若前端把它放進 <a href> 就成為 XSS 的入口。
ALLOWED_SCHEMES = ("http", "https")

# 上限取自 IE 的歷史 URL 長度限制，用來擋下把 queue 當儲存空間的超長字串。
MAX_URL_LENGTH = 2048
MAX_NOTE_LENGTH = 200

STATUSES = ("pending", "done", "skipped")


class QueueIn(BaseModel):
    url: str = Field(description="單集網址（http/https）")
    note: str | None = Field(None, description="備註，例如想聽的原因")
    project_ids: list[UUID] = Field(
        default_factory=list, max_length=20, description="本機處理完成後歸入的研究專案（可多個）"
    )


class QueueOut(BaseModel):
    id: str
    url: str
    episode_guid: str | None
    title: str | None
    note: str | None
    status: str
    created_at: str | None
    processed_at: str | None
    project_ids: list[str]


def _row_to_item(row: tuple) -> dict:
    return {
        "id": str(row[0]),
        "url": row[1],
        "episode_guid": row[2],
        "title": row[3],
        "note": row[4],
        "status": row[5],
        "created_at": row[6].isoformat() if row[6] else None,
        "processed_at": row[7].isoformat() if row[7] else None,
        "project_ids": [str(p) for p in (row[8] or [])],
    }


COLUMNS = "id, url, episode_guid, title, note, status, created_at, processed_at, project_ids"


def _validate_url(url: str) -> str:
    """只檢查是否為合法的 http(s) 網址。

    不檢查平台：解析責任在本機端 resolver，那裡才知道支援哪些平台，
    平台擴充時不必同步改動手機端。

    Raises:
        HTTPException: 網址為空、過長、scheme 不允許或缺少網域。
    """
    url = url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="請輸入網址")
    if len(url) > MAX_URL_LENGTH:
        raise HTTPException(status_code=400, detail=f"網址過長（上限 {MAX_URL_LENGTH} 字元）")

    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="網址格式不正確") from exc

    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise HTTPException(status_code=400, detail="網址須以 http:// 或 https:// 開頭")
    if not parsed.netloc:
        raise HTTPException(status_code=400, detail="網址缺少網域")

    return url


def _validate_note(note: str | None) -> str | None:
    """備註只做長度上限；內容跳脫由前端顯示時負責。"""
    note = (note or "").strip()
    if not note:
        return None
    if len(note) > MAX_NOTE_LENGTH:
        raise HTTPException(status_code=400, detail=f"備註過長（上限 {MAX_NOTE_LENGTH} 字元）")
    return note


def insert_item(cur, url: str, note: str | None, added_by: str | None, project_ids: list[str]) -> tuple[tuple, bool]:
    """寫入一筆待處理；網址與備註須先經 _validate_url／_validate_note。

    Telegram 分享入口（routers/telegram.py）共用此函式，重複判斷與手機網頁一致。
    呼叫端負責 commit。

    Returns:
        (資料列, 是否為新增)；同網址已有 pending 時回傳既有那筆與 False。

    Raises:
        HTTPException: 409，這集已經處理過。
    """
    # 已處理過的單集不必再排一次，直接擋下並說明原因。
    cur.execute("select title from episodes where source_url = %s", (url,))
    existing = cur.fetchone()
    if existing:
        raise HTTPException(status_code=409, detail=f"這集已經處理過了：{existing[0]}")

    # queue_url_pending_idx 保證同一網址只有一筆 pending；
    # 重複貼上時回傳既有那筆，讓前端當成成功而非錯誤。
    cur.execute(
        f"insert into queue (url, note, added_by, project_ids) values (%s, %s, %s, %s::uuid[])"
        f" on conflict do nothing returning {COLUMNS}",
        (url, note, added_by, project_ids),
    )
    row = cur.fetchone()
    if row is not None:
        return row, True

    # 重複貼上時把這次選的專案併進既有那筆，不覆蓋先前選的
    cur.execute(
        "update queue set project_ids ="
        " array(select distinct unnest(project_ids || %s::uuid[]))"
        f" where url = %s and status = 'pending' returning {COLUMNS}",
        (project_ids, url),
    )
    return cur.fetchone(), False


@router.post("/queue", response_model=QueueOut, status_code=201, summary="貼上網址加入待處理")
def add_to_queue(body: QueueIn, user: dict = Depends(get_current_user)) -> dict:
    url = _validate_url(body.url)
    note = _validate_note(body.note)

    with get_connection() as conn:
        with conn.cursor() as cur:
            row, _ = insert_item(cur, url, note, user["id"], [str(p) for p in body.project_ids])
            conn.commit()

    return _row_to_item(row)


@router.get("/queue", response_model=list[QueueOut], summary="待處理清單")
def list_queue(
    status: str = Query("pending", description="pending／done／skipped，all 為全部"),
    limit: int = Query(100, ge=1, le=500),
) -> list[dict]:
    if status != "all" and status not in STATUSES:
        raise HTTPException(status_code=400, detail=f"status 需為 {'／'.join(STATUSES)} 或 all")

    where = "" if status == "all" else "where status = %s"
    params: tuple = () if status == "all" else (status,)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"select {COLUMNS} from queue {where} order by created_at desc limit %s",
                (*params, limit),
            )
            rows = cur.fetchall()

    return [_row_to_item(row) for row in rows]


@router.delete("/queue/{item_id}", status_code=204, summary="從待處理移除")
def remove_from_queue(item_id: str) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("delete from queue where id = %s", (item_id,))
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="查無此項目")
            conn.commit()
