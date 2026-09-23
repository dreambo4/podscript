import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from jose import JWTError, jwt

SECRET_KEY = os.environ.get("JWT_SECRET", "") + os.environ.get("JWT_SALT", "")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_DAYS = 30

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")


def create_access_token(user_id: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS)
    return jwt.encode({"sub": user_id, "exp": expire}, SECRET_KEY, algorithm=ALGORITHM)


def decode_access_token(token: str) -> Optional[str]:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload.get("sub")
    except JWTError:
        return None


async def verify_google_id_token(id_token: str) -> Optional[dict]:
    """驗證 Google id_token，回傳 sub / email / name。"""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://oauth2.googleapis.com/tokeninfo",
            params={"id_token": id_token},
        )
    if resp.status_code != 200:
        return None
    data = resp.json()
    if GOOGLE_CLIENT_ID and data.get("aud") != GOOGLE_CLIENT_ID:
        return None
    return {"sub": data.get("sub"), "email": data.get("email"), "name": data.get("name")}
