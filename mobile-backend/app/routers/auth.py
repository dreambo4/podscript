from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..auth import create_access_token, verify_google_id_token
from ..database import get_connection

router = APIRouter(tags=["認證"])


class GoogleAuthIn(BaseModel):
    id_token: str


class AuthOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str
    name: str


@router.post("/auth/google", response_model=AuthOut, summary="Google 登入")
async def google_login(body: GoogleAuthIn) -> AuthOut:
    payload = await verify_google_id_token(body.id_token)
    if not payload or not payload.get("sub"):
        raise HTTPException(status_code=401, detail="Invalid Google token")

    sub = payload["sub"]
    email = payload.get("email", "")

    with get_connection() as conn:
        with conn.cursor() as cur:
            # 先用 google_sub 查，再 fallback 到 email（首次登入時 sub 尚未存入）
            cur.execute("select id, name, google_sub from users where google_sub = %s", (sub,))
            row = cur.fetchone()

            if not row and email:
                cur.execute("select id, name, google_sub from users where email = %s", (email,))
                row = cur.fetchone()

            if not row:
                raise HTTPException(status_code=403, detail="Unauthorized: account not registered")

            user_id, name, existing_sub = row

            # 首次登入：寫入 google_sub（之後直接用 sub 查，不依賴 email）
            if not existing_sub:
                cur.execute("update users set google_sub = %s where id = %s", (sub, user_id))
                conn.commit()

    return AuthOut(
        access_token=create_access_token(str(user_id)),
        user_id=str(user_id),
        name=name,
    )
