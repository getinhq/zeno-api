"""Auth HTTP endpoints — /login, /refresh, /logout, /me.

Refresh tokens are blacklisted on logout via Redis key
``auth:blacklist:<jti>`` with TTL = remaining lifetime of the token.
If Redis is unavailable the blacklist fails open (logs, 200) rather than
refusing all logouts — the token will still expire naturally.
"""
from __future__ import annotations

import logging
from typing import Any, Optional
from uuid import UUID

import jwt
from fastapi import APIRouter, Body, Depends, HTTPException, status

from app.auth import service as auth_service
from app.auth.deps import CurrentUser, get_current_user
from app.db import acquire
from app.redis_conn import get_redis

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

_BLACKLIST_PREFIX = "auth:blacklist:"


async def _is_blacklisted(jti: str) -> bool:
    if not jti:
        return False
    try:
        r = await get_redis()
        return bool(await r.exists(_BLACKLIST_PREFIX + jti))
    except Exception:  # noqa: BLE001
        return False


async def _blacklist(jti: str, ttl_seconds: int) -> None:
    try:
        r = await get_redis()
        await r.set(_BLACKLIST_PREFIX + jti, "1", ex=max(ttl_seconds, 1))
    except Exception as exc:  # noqa: BLE001
        log.warning("auth.blacklist redis unavailable: %s", exc)


@router.post("/login")
async def login(body: dict = Body(...)) -> dict:
    """Exchange username + password for an access + refresh token pair."""
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if not username or not password:
        raise HTTPException(status_code=400, detail="username and password are required")

    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, email, username, password_hash, name, role, app_role, is_active
            FROM users
            WHERE username = $1
            LIMIT 1
            """,
            username,
        )

    if not row or not row["is_active"]:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not auth_service.verify_password(password, row["password_hash"] or ""):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    access, _ = auth_service.issue_access_token(
        row["id"], row["username"], row["app_role"]
    )
    refresh, _, _ = auth_service.issue_refresh_token(row["id"])

    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "bearer",
        "user": {
            "id": str(row["id"]),
            "username": row["username"],
            "email": row["email"],
            "name": row["name"],
            "role": row["role"],
            "app_role": row["app_role"],
        },
    }


@router.post("/refresh")
async def refresh(body: dict = Body(...)) -> dict:
    """Exchange a still-valid refresh token for a new access token."""
    token = (body.get("refresh_token") or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="refresh_token is required")
    try:
        payload = auth_service.decode_token(token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Refresh token expired")
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    if payload.get("typ") != "refresh":
        raise HTTPException(status_code=401, detail="Wrong token type")

    jti = str(payload.get("jti") or "")
    if await _is_blacklisted(jti):
        raise HTTPException(status_code=401, detail="Refresh token revoked")

    try:
        uid = UUID(str(payload.get("sub")))
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Invalid refresh token subject")

    async with acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, username, app_role, is_active FROM users WHERE id = $1",
            uid,
        )
    if not row or not row["is_active"]:
        raise HTTPException(status_code=401, detail="User is inactive")

    access, _ = auth_service.issue_access_token(
        row["id"], row["username"], row["app_role"]
    )
    return {"access_token": access, "token_type": "bearer"}


@router.post("/logout")
async def logout(body: dict = Body(default_factory=dict)) -> dict:
    """Revoke the supplied refresh token (best-effort; blacklist in Redis)."""
    token = (body.get("refresh_token") or "").strip() if body else ""
    if not token:
        return {"ok": True}
    try:
        payload = auth_service.decode_token(token)
    except jwt.PyJWTError:
        return {"ok": True}
    jti = str(payload.get("jti") or "")
    exp = int(payload.get("exp") or 0)
    import time

    ttl = max(exp - int(time.time()), 1)
    if jti:
        await _blacklist(jti, ttl)
    return {"ok": True}


@router.get("/me")
async def me(user: CurrentUser = Depends(get_current_user)) -> dict:
    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, email, username, name, role, app_role, is_active, department
            FROM users
            WHERE id = $1
            """,
            user.id,
        )
    if not row or not row["is_active"]:
        raise HTTPException(status_code=401, detail="User is inactive")
    return {
        "id": str(row["id"]),
        "username": row["username"],
        "email": row["email"],
        "name": row["name"],
        "role": row["role"],
        "app_role": row["app_role"],
        "department": row["department"],
    }
