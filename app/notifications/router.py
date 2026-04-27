"""Notifications HTTP endpoints — /api/v1/notifications.

List: returns notifications for a project visible to the caller.
- Management roles see personal rows AND project-wide rows (``user_id IS NULL``).
- Artists see only personal rows.

Mark-read: ``POST /mark-read`` with a body of ``{ids: [...]}`` or
``{all_for_project: project_id}``.
"""
from __future__ import annotations

import json
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from app.auth.deps import CurrentUser, require_current_user
from app.db import acquire

router = APIRouter(prefix="/api/v1/notifications", tags=["notifications"])


def _parse_payload(value: Any) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


@router.get("")
async def list_notifications(
    project_id: UUID = Query(...),
    unread_only: bool = Query(False),
    limit: int = Query(50, ge=1, le=500),
    user: CurrentUser = Depends(require_current_user),
) -> dict:
    role = (user.app_role or "").lower()
    is_mgmt = role in ("pipeline", "supervisor", "production")
    audience_clause = (
        "(n.user_id IS NULL OR n.user_id = $2)" if is_mgmt else "n.user_id = $2"
    )
    where = f"n.project_id = $1 AND {audience_clause}"
    list_where = where + (" AND n.read_at IS NULL" if unread_only else "")

    async with acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT n.id, n.project_id, n.user_id, n.kind, n.payload,
                   n.read_at, n.created_at
            FROM notifications n
            WHERE {list_where}
            ORDER BY n.created_at DESC
            LIMIT $3
            """,
            project_id,
            user.id,
            limit,
        )
        unread_row = await conn.fetchrow(
            f"""
            SELECT COUNT(*)::INT AS n
            FROM notifications n
            WHERE {where} AND n.read_at IS NULL
            """,
            project_id,
            user.id,
        )

    return {
        "items": [
            {
                "id": str(r["id"]),
                "project_id": str(r["project_id"]) if r["project_id"] else None,
                "user_id": str(r["user_id"]) if r["user_id"] else None,
                "kind": r["kind"],
                "payload": _parse_payload(r["payload"]),
                "read_at": r["read_at"].isoformat() if r["read_at"] else None,
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ],
        "unread_count": int(unread_row["n"]) if unread_row else 0,
    }


@router.post("/mark-read")
async def mark_read(
    body: dict = Body(default_factory=dict),
    user: CurrentUser = Depends(require_current_user),
) -> dict:
    ids = body.get("ids") if body else None
    all_for_project = body.get("all_for_project") if body else None

    async with acquire() as conn:
        if ids:
            await conn.execute(
                """
                UPDATE notifications
                SET read_at = NOW()
                WHERE id = ANY($1::uuid[])
                  AND (user_id IS NULL OR user_id = $2)
                  AND read_at IS NULL
                """,
                ids,
                user.id,
            )
        elif all_for_project:
            await conn.execute(
                """
                UPDATE notifications
                SET read_at = NOW()
                WHERE project_id = $1
                  AND (user_id IS NULL OR user_id = $2)
                  AND read_at IS NULL
                """,
                all_for_project,
                user.id,
            )
        else:
            raise HTTPException(status_code=400, detail="Provide ids[] or all_for_project")
    return {"ok": True}
