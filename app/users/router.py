"""Users API — list users (for assignee pickers), create, deactivate.

Writes are restricted to Pipeline role. Reads are open to any authenticated
caller (so the Task create modal can fetch the artist list). When
``ENABLE_AUTH`` is off the dependency returns an anonymous pipeline stub so
legacy scripts keep working.
"""
from __future__ import annotations

from typing import Any, Optional
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Body, Depends, HTTPException, Query

from app.auth import service as auth_service
from app.auth.deps import CurrentUser, require_current_user, require_role
from app.db import acquire

router = APIRouter(prefix="/api/v1/users", tags=["users"])


def _serialize(row: asyncpg.Record) -> dict:
    return {
        "id": str(row["id"]),
        "email": row["email"],
        "username": row["username"],
        "name": row["name"],
        "role": row["role"],
        "app_role": row["app_role"],
        "department": row["department"],
        "is_active": bool(row["is_active"]),
    }


@router.get("")
async def list_users(
    app_role: Optional[str] = Query(None, description="Filter by app_role (artist|pipeline|supervisor|production)"),
    is_active: Optional[bool] = Query(True),
    _user: CurrentUser = Depends(require_current_user),
) -> list[dict]:
    conds: list[str] = []
    params: list[Any] = []
    if app_role:
        conds.append(f"app_role = ${len(params) + 1}")
        params.append(app_role)
    if is_active is not None:
        conds.append(f"is_active = ${len(params) + 1}")
        params.append(is_active)
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    async with acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT id, email, username, name, role, app_role, department, is_active
            FROM users
            {where}
            ORDER BY COALESCE(name, username, email)
            """,
            *params,
        )
    return [_serialize(r) for r in rows]


@router.get("/{user_id}")
async def get_user(user_id: UUID, _user: CurrentUser = Depends(require_current_user)) -> dict:
    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, email, username, name, role, app_role, department, is_active
            FROM users WHERE id = $1
            """,
            user_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    return _serialize(row)


@router.post("")
async def create_user(
    body: dict = Body(...),
    _admin: CurrentUser = Depends(require_role("pipeline")),
) -> dict:
    """Create a user. Pipeline role only.

    Required: username, email, password, app_role.
    Optional: name, role (legacy enum), department.
    """
    username = (body.get("username") or "").strip()
    email = (body.get("email") or "").strip()
    password = body.get("password") or ""
    app_role = (body.get("app_role") or "").strip()
    if not username or not email or not password:
        raise HTTPException(status_code=400, detail="username, email and password are required")
    if app_role not in ("artist", "pipeline", "supervisor", "production"):
        raise HTTPException(status_code=400, detail="app_role must be one of artist|pipeline|supervisor|production")

    legacy_role = body.get("role")
    name = body.get("name")
    department = body.get("department")

    pw_hash = auth_service.hash_password(password)
    async with acquire() as conn:
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO users (email, username, password_hash, name, role, app_role, department, is_active)
                VALUES ($1, $2, $3, $4, $5, $6, $7, TRUE)
                RETURNING id, email, username, name, role, app_role, department, is_active
                """,
                email,
                username,
                pw_hash,
                name,
                legacy_role,
                app_role,
                department,
            )
        except asyncpg.UniqueViolationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _serialize(row)


@router.patch("/{user_id}")
async def update_user(
    user_id: UUID,
    body: dict = Body(...),
    _admin: CurrentUser = Depends(require_role("pipeline")),
) -> dict:
    fields: list[str] = []
    params: list[Any] = []
    for key in ("email", "username", "name", "role", "app_role", "department", "is_active"):
        if key in body:
            fields.append(f"{key} = ${len(params) + 1}")
            params.append(body[key])
    if "password" in body and body["password"]:
        fields.append(f"password_hash = ${len(params) + 1}")
        params.append(auth_service.hash_password(body["password"]))
    if not fields:
        raise HTTPException(status_code=400, detail="No updatable fields provided")

    params.append(user_id)
    query = (
        "UPDATE users SET "
        + ", ".join(fields)
        + " WHERE id = $"
        + str(len(params))
        + " RETURNING id, email, username, name, role, app_role, department, is_active"
    )
    async with acquire() as conn:
        try:
            row = await conn.fetchrow(query, *params)
        except asyncpg.UniqueViolationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    return _serialize(row)
