from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .auth import decode_access_token
from .database import get_connection

bearer = HTTPBearer()


def get_current_user(credentials: HTTPAuthorizationCredentials = Security(bearer)) -> dict:
    user_id = decode_access_token(credentials.credentials)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("select id, name, email from users where id = %s", (user_id,))
            row = cur.fetchone()

    if not row:
        raise HTTPException(status_code=401, detail="User not found")
    return {"id": str(row[0]), "name": row[1], "email": row[2]}
