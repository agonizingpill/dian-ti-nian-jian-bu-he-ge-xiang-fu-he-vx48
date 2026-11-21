import hashlib
import os
from datetime import datetime, timedelta, timezone
from fastapi import Depends, HTTPException, Request
import jwt
from .db import connection

SECRET = os.getenv("JWT_SECRET", "gb1066-demo-secret")
ALGORITHM = "HS256"

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def create_token(user: dict) -> str:
    payload = {"sub": str(user["id"]), "email": user["email"], "name": user["name"], "role": user["role"], "exp": datetime.now(timezone.utc) + timedelta(hours=10)}
    return jwt.encode(payload, SECRET, algorithm=ALGORITHM)

def current_user(request: Request):
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(401, "需要登录")
    try:
        return jwt.decode(header[7:], SECRET, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        raise HTTPException(401, "登录已过期或无效")

def require_roles(*roles):
    def checker(user=Depends(current_user)):
        if user["role"] not in roles:
            raise HTTPException(403, f"当前角色无权执行此操作，需要 {', '.join(roles)}")
        return user
    return checker

def user_row(user_id: int):
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id,email,name,role FROM users WHERE id=%s", (user_id,))
            return cur.fetchone()
