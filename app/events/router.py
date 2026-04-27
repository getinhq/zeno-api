"""Activity feed endpoint — /api/v1/events.

Server-side role filtering (plan §11):
- Management roles (pipeline/supervisor/production) see all events for a
  project.
- Artists see only events that involve them directly: tasks/issues they're
  assigned to or collaborating on, plus any version they published.
"""
from __future__ import annotations

import json
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth.deps import CurrentUser, require_current_user
from app.db import acquire

router = APIRouter(prefix="/api/v1/events", tags=["events"])


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
async def list_events(
    project_id: UUID = Query(...),
    limit: int = Query(50, ge=1, le=500),
    task_id: Optional[UUID] = Query(None),
    user: CurrentUser = Depends(require_current_user),
) -> list[dict]:
    role = (user.app_role or "").lower()

    if role in ("pipeline", "supervisor", "production"):
        async with acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT e.id, e.project_id, e.actor_id, e.kind, e.payload, e.created_at,
                       u.username AS actor_username, u.name AS actor_name
                FROM events e
                LEFT JOIN users u ON e.actor_id = u.id
                WHERE e.project_id = $1
                  AND ($3::uuid IS NULL OR e.payload->>'task_id' = $3::text)
                ORDER BY e.created_at DESC
                LIMIT $2
                """,
                project_id,
                limit,
                task_id,
            )
    else:
        # Artist / unknown: only events tied to this user's tasks/issues.
        # Guard the ``::uuid`` casts: a non-UUID payload string would raise and
        # tank the whole query. Matching against the text representation of the
        # user's task/issue ids sidesteps that and keeps the feed resilient.
        async with acquire() as conn:
            rows = await conn.fetch(
                """
                WITH my_tasks AS (
                    SELECT task_id::text AS tid FROM task_assignees WHERE user_id = $2
                    UNION
                    SELECT task_id::text AS tid FROM task_collaborators WHERE user_id = $2
                    UNION
                    SELECT id::text AS tid FROM tasks WHERE assignee_id = $2
                ),
                my_issues AS (
                    SELECT id::text AS iid FROM issues
                    WHERE assignee_id = $2 OR reporter_id = $2
                )
                SELECT e.id, e.project_id, e.actor_id, e.kind, e.payload, e.created_at,
                       u.username AS actor_username, u.name AS actor_name
                FROM events e
                LEFT JOIN users u ON e.actor_id = u.id
                WHERE e.project_id = $1
                  AND ($4::uuid IS NULL OR e.payload->>'task_id' = $4::text)
                  AND (
                      e.actor_id = $2
                      OR (e.payload->>'task_id') IN (SELECT tid FROM my_tasks)
                      OR (e.payload->>'issue_id') IN (SELECT iid FROM my_issues)
                  )
                ORDER BY e.created_at DESC
                LIMIT $3
                """,
                project_id,
                user.id,
                limit,
                task_id,
            )

    return [
        {
            "id": str(r["id"]),
            "project_id": str(r["project_id"]) if r["project_id"] else None,
            "actor_id": str(r["actor_id"]) if r["actor_id"] else None,
            "actor_username": r["actor_username"],
            "actor_name": r["actor_name"],
            "kind": r["kind"],
            "payload": _parse_payload(r["payload"]),
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]
