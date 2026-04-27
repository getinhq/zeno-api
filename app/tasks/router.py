"""Tasks API — list, get, create, update, plus stats and personal task feed."""
from __future__ import annotations

from datetime import date, datetime
import json
from typing import Any, Optional
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query

from app.auth.deps import optional_current_user, require_role
from app.db import acquire
from app.events import log as events_log
from app.notifications import service as notifications_service

router = APIRouter(prefix="/api/v1", tags=["tasks"])


STATUS_ALIASES = {
    "not_started": "todo",
    "todo": "todo",
    "in_progress": "in_progress",
    "in_review": "review",
    "review": "review",
    "completed": "done",
    "done": "done",
    "blocked": "blocked",
}


def _normalize_status(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    return STATUS_ALIASES.get(str(value).strip().lower(), value)


def _display_status(db_status: Optional[str]) -> Optional[str]:
    """Translate legacy DB status values into the UI's new vocabulary."""
    if not db_status:
        return db_status
    return {
        "todo": "not_started",
        "review": "in_review",
        "done": "completed",
    }.get(db_status, db_status)


def _json_metadata(value: Any) -> dict:
    """Normalize Postgres json/jsonb (dict, str, or legacy shapes) to a dict."""
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


def _coerce_due_date(value: Any) -> date | datetime | None:
    """Accept ISO date/datetime strings from UI and convert for asyncpg."""
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        # Browser date input sends YYYY-MM-DD.
        try:
            return date.fromisoformat(s)
        except ValueError:
            pass
        # Also allow full ISO datetimes.
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail="due_date must be an ISO date or datetime",
            ) from exc
    raise HTTPException(status_code=400, detail="due_date must be date/datetime string")


def _project_from_task_filter_clause(alias: str = "t") -> str:
    """SQL fragment joining tasks -> project via shot or asset, if project_id is NULL."""
    return f"""
        LEFT JOIN shots s_pf ON {alias}.shot_id = s_pf.id
        LEFT JOIN sequences seq_pf ON s_pf.sequence_id = seq_pf.id
        LEFT JOIN episodes e_pf ON seq_pf.episode_id = e_pf.id
        LEFT JOIN assets a_pf ON {alias}.asset_id = a_pf.id
    """


def _resolve_project_expr(alias: str = "t") -> str:
    return f"COALESCE({alias}.project_id, e_pf.project_id, a_pf.project_id)"


async def _load_assignees(conn, task_ids: list[UUID]) -> dict[str, list[str]]:
    if not task_ids:
        return {}
    rows = await conn.fetch(
        "SELECT task_id, user_id FROM task_assignees WHERE task_id = ANY($1::uuid[])",
        task_ids,
    )
    out: dict[str, list[str]] = {}
    for r in rows:
        out.setdefault(str(r["task_id"]), []).append(str(r["user_id"]))
    return out


async def _load_collaborators(conn, task_ids: list[UUID]) -> dict[str, list[str]]:
    if not task_ids:
        return {}
    rows = await conn.fetch(
        "SELECT task_id, user_id FROM task_collaborators WHERE task_id = ANY($1::uuid[])",
        task_ids,
    )
    out: dict[str, list[str]] = {}
    for r in rows:
        out.setdefault(str(r["task_id"]), []).append(str(r["user_id"]))
    return out


def _serialize_task(row, assignees: list[str], collaborators: list[str]) -> dict:
    return {
        "id": str(row["id"]),
        "project_id": str(row["project_id"]) if row.get("project_id") else None,
        "shot_id": str(row["shot_id"]) if row["shot_id"] else None,
        "asset_id": str(row["asset_id"]) if row["asset_id"] else None,
        "type": row["type"],
        "title": row.get("title"),
        "description": row.get("description"),
        "assignee_id": str(row["assignee_id"]) if row["assignee_id"] else None,
        "reviewer_id": str(row["reviewer_id"]) if row.get("reviewer_id") else None,
        "assignees": assignees,
        "collaborators": collaborators,
        "status": _display_status(row["status"]),
        "status_raw": row["status"],
        "estimated_hours": float(row["estimated_hours"]) if row["estimated_hours"] is not None else None,
        "actual_hours": float(row["actual_hours"]) if row["actual_hours"] is not None else None,
        "due_date": row["due_date"].isoformat() if row["due_date"] else None,
        "metadata": _json_metadata(row["metadata"]),
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


@router.get("/tasks")
async def list_tasks(
    project_id: Optional[UUID] = Query(None),
    asset_id: Optional[UUID] = Query(None),
    shot_id: Optional[UUID] = Query(None),
    type: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    # Sentinel ``none`` = unassigned (no direct assignee and no task_assignees rows).
    assignee_id: Optional[str] = Query(None),
    department: Optional[str] = Query(
        None,
        description="Filter tasks where any direct assignee or task_assignees member has this department",
    ),
) -> list[dict]:
    """List tasks with optional filters."""
    base = """
        SELECT t.id, t.project_id, t.shot_id, t.asset_id, t.type, t.title, t.description,
               t.assignee_id, t.reviewer_id, t.status, t.estimated_hours, t.actual_hours,
               t.due_date, t.metadata, t.created_at, t.updated_at
        FROM tasks t
    """
    joins: list[str] = []
    conditions: list[str] = []
    params: list[Any] = []

    if project_id:
        joins.append(_project_from_task_filter_clause())
        conditions.append(f"{_resolve_project_expr()} = ${len(params) + 1}")
        params.append(project_id)
    if shot_id:
        conditions.append(f"t.shot_id = ${len(params) + 1}")
        params.append(shot_id)
    if asset_id:
        conditions.append(f"t.asset_id = ${len(params) + 1}")
        params.append(asset_id)
    if type:
        conditions.append(f"t.type = ${len(params) + 1}")
        params.append(type)
    if status:
        conditions.append(f"t.status = ${len(params) + 1}")
        params.append(_normalize_status(status))
    if assignee_id:
        token = str(assignee_id).strip().lower()
        if token == "none":
            conditions.append(
                "t.assignee_id IS NULL AND NOT EXISTS "
                "(SELECT 1 FROM task_assignees ta WHERE ta.task_id = t.id)"
            )
        else:
            try:
                uid = UUID(str(assignee_id))
            except (ValueError, TypeError) as exc:
                raise HTTPException(
                    status_code=400,
                    detail="assignee_id must be a UUID or 'none'",
                ) from exc
            conditions.append(
                f"(t.assignee_id = ${len(params) + 1} OR EXISTS "
                f"(SELECT 1 FROM task_assignees ta WHERE ta.task_id = t.id AND ta.user_id = ${len(params) + 1}))"
            )
            params.append(uid)
    if department:
        conditions.append(
            f"""EXISTS (
                SELECT 1 FROM users u_dept
                WHERE LOWER(COALESCE(u_dept.department, '')) = ${len(params) + 1}
                  AND (
                    u_dept.id = t.assignee_id
                    OR EXISTS (SELECT 1 FROM task_assignees ta2
                               WHERE ta2.task_id = t.id AND ta2.user_id = u_dept.id)
                  )
            )"""
        )
        params.append(department.strip().lower())

    query = base + " ".join(joins)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY t.created_at DESC"

    async with acquire() as conn:
        rows = await conn.fetch(query, *params)
        task_ids = [r["id"] for r in rows]
        assignees = await _load_assignees(conn, task_ids)
        collaborators = await _load_collaborators(conn, task_ids)

    return [
        _serialize_task(r, assignees.get(str(r["id"]), []), collaborators.get(str(r["id"]), []))
        for r in rows
    ]


@router.get("/tasks/mine")
async def list_my_tasks(
    project_id: Optional[UUID] = Query(None),
    current=Depends(optional_current_user),
) -> list[dict]:
    """
    Tasks the current user is directly involved in (assignee, additional assignee,
    reviewer, or collaborator). When auth is disabled, returns every task for
    the project — matches legacy dev behaviour.
    """
    if not current or not current.id:
        # Auth disabled or anonymous — fall back to unfiltered list so dev setups still work.
        return await list_tasks(project_id=project_id)

    params: list[Any] = [current.id]
    conditions = [
        "("
        "t.assignee_id = $1 OR t.reviewer_id = $1"
        " OR EXISTS (SELECT 1 FROM task_assignees ta WHERE ta.task_id = t.id AND ta.user_id = $1)"
        " OR EXISTS (SELECT 1 FROM task_collaborators tc WHERE tc.task_id = t.id AND tc.user_id = $1)"
        ")"
    ]
    joins: list[str] = []
    if project_id:
        joins.append(_project_from_task_filter_clause())
        conditions.append(f"{_resolve_project_expr()} = ${len(params) + 1}")
        params.append(project_id)

    query = (
        "SELECT t.id, t.project_id, t.shot_id, t.asset_id, t.type, t.title, t.description, "
        "t.assignee_id, t.reviewer_id, t.status, t.estimated_hours, t.actual_hours, "
        "t.due_date, t.metadata, t.created_at, t.updated_at FROM tasks t "
        + " ".join(joins)
        + " WHERE "
        + " AND ".join(conditions)
        + " ORDER BY t.created_at DESC"
    )

    async with acquire() as conn:
        rows = await conn.fetch(query, *params)
        task_ids = [r["id"] for r in rows]
        assignees = await _load_assignees(conn, task_ids)
        collaborators = await _load_collaborators(conn, task_ids)

    return [
        _serialize_task(r, assignees.get(str(r["id"]), []), collaborators.get(str(r["id"]), []))
        for r in rows
    ]


@router.get("/tasks/stats")
async def task_stats(
    project_id: UUID = Query(..., description="Project scope"),
    current=Depends(optional_current_user),
    mine: bool = Query(False, description="Return personal stats (Artist view)"),
) -> dict:
    """Aggregate task counters for the overview dashboard."""
    params: list[Any] = [project_id]
    mine_clause = ""
    joins = _project_from_task_filter_clause()
    if mine and current and current.id:
        params.append(current.id)
        mine_clause = (
            " AND ("
            "t.assignee_id = $2 OR t.reviewer_id = $2"
            " OR EXISTS (SELECT 1 FROM task_assignees ta WHERE ta.task_id = t.id AND ta.user_id = $2)"
            " OR EXISTS (SELECT 1 FROM task_collaborators tc WHERE tc.task_id = t.id AND tc.user_id = $2)"
            ")"
        )

    query = f"""
        SELECT
            COUNT(*) FILTER (WHERE t.status = 'todo') AS not_started,
            COUNT(*) FILTER (WHERE t.status = 'in_progress') AS in_progress,
            COUNT(*) FILTER (WHERE t.status = 'review') AS in_review,
            COUNT(*) FILTER (WHERE t.status = 'done') AS completed,
            COUNT(*) FILTER (WHERE t.status = 'blocked') AS blocked,
            COUNT(*) FILTER (
                WHERE t.due_date IS NOT NULL
                  AND t.due_date < NOW()
                  AND t.status NOT IN ('done')
            ) AS overdue,
            COUNT(*) FILTER (
                WHERE t.assignee_id IS NULL
                  AND NOT EXISTS (SELECT 1 FROM task_assignees ta WHERE ta.task_id = t.id)
            ) AS unassigned,
            COUNT(*) AS total
        FROM tasks t
        {joins}
        WHERE {_resolve_project_expr()} = $1{mine_clause}
    """

    async with acquire() as conn:
        row = await conn.fetchrow(query, *params)

    total = int(row["total"] or 0)
    completed = int(row["completed"] or 0)
    open_tasks = total - completed
    completion_pct = (completed / total * 100.0) if total else 0.0
    return {
        "project_id": str(project_id),
        "scope": "mine" if mine else "project",
        "not_started": int(row["not_started"] or 0),
        "in_progress": int(row["in_progress"] or 0),
        "in_review": int(row["in_review"] or 0),
        "completed": completed,
        "blocked": int(row["blocked"] or 0),
        "overdue": int(row["overdue"] or 0),
        "unassigned": int(row["unassigned"] or 0),
        "open": open_tasks,
        "total": total,
        "completion_pct": round(completion_pct, 1),
    }


@router.get("/tasks/{task_id}")
async def get_task(task_id: UUID = Path(...)) -> dict:
    """Get one task by id."""
    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, project_id, shot_id, asset_id, type, title, description,
                   assignee_id, reviewer_id, status, estimated_hours, actual_hours,
                   due_date, metadata, created_at, updated_at
            FROM tasks
            WHERE id = $1
            """,
            task_id,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")
        assignees = (await _load_assignees(conn, [row["id"]])).get(str(row["id"]), [])
        collaborators = (await _load_collaborators(conn, [row["id"]])).get(str(row["id"]), [])
    return _serialize_task(row, assignees, collaborators)


@router.get("/tasks/{task_id}/versions")
async def list_task_versions(task_id: UUID = Path(...)) -> list[dict]:
    """List versions linked to a task, newest first."""
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT v.id, v.asset_id, v.representation, v.pipeline_stage, v.version_number,
                   v.content_id, v.filename, v.size_bytes, v.feedback, v.status,
                   v.published_at, v.created_at
            FROM versions v
            WHERE v.task_id = $1
            ORDER BY v.version_number DESC, v.created_at DESC
            """,
            task_id,
        )
    return [
        {
            "id": str(r["id"]),
            "asset_id": str(r["asset_id"]) if r["asset_id"] else None,
            "representation": r["representation"],
            "pipeline_stage": str(r["pipeline_stage"] or ""),
            "version_number": int(r["version_number"]),
            "content_id": r["content_id"],
            "filename": r["filename"],
            "size": r["size_bytes"],
            "feedback": r["feedback"],
            "status": r["status"],
            "published_at": r["published_at"].isoformat() if r["published_at"] else None,
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]


@router.post("/tasks")
async def create_task(
    body: dict = Body(...),
    current=Depends(require_role("pipeline", "supervisor", "production")),
) -> dict:
    """
    Create a task. Management-only. Body supports:
        type (required), title, description, project_id, shot_id, asset_id,
        assignee_id (legacy, optional), assignees=[uuid], collaborators=[uuid],
        reviewer_id, status, estimated_hours, due_date, metadata
    New tasks always land in ``not_started`` unless caller overrides.
    """
    t_type = body.get("type")
    if not t_type:
        raise HTTPException(status_code=400, detail="type is required")

    title = body.get("title")
    description = body.get("description")
    project_id = body.get("project_id")
    shot_id = body.get("shot_id")
    asset_id = body.get("asset_id")
    reviewer_id = body.get("reviewer_id")
    status = _normalize_status(body.get("status", "not_started"))
    estimated_hours = body.get("estimated_hours")
    due_date = _coerce_due_date(body.get("due_date"))
    metadata = body.get("metadata") or {}
    assignees: list[str] = list(body.get("assignees") or [])
    collaborators: list[str] = list(body.get("collaborators") or [])
    assignee_id = body.get("assignee_id") or (assignees[0] if assignees else None)

    async with acquire() as conn:
        async with conn.transaction():
            try:
                row = await conn.fetchrow(
                    """
                    INSERT INTO tasks (
                        project_id, shot_id, asset_id, type, title, description,
                        assignee_id, reviewer_id, status, estimated_hours, due_date, metadata
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::timestamptz, $12::jsonb)
                    RETURNING id, project_id, shot_id, asset_id, type, title, description,
                              assignee_id, reviewer_id, status, estimated_hours, actual_hours,
                              due_date, metadata, created_at, updated_at
                    """,
                    project_id,
                    shot_id,
                    asset_id,
                    t_type,
                    title,
                    description,
                    assignee_id,
                    reviewer_id,
                    status,
                    estimated_hours,
                    due_date,
                    json.dumps(metadata) if metadata else "{}",
                )
            except asyncpg.ForeignKeyViolationError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e

            task_id = row["id"]
            all_assignee_ids = {str(a) for a in assignees}
            if assignee_id:
                all_assignee_ids.add(str(assignee_id))
            for uid in all_assignee_ids:
                await conn.execute(
                    "INSERT INTO task_assignees(task_id, user_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                    task_id,
                    uid,
                )
            for uid in {str(c) for c in collaborators}:
                await conn.execute(
                    "INSERT INTO task_collaborators(task_id, user_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                    task_id,
                    uid,
                )

            resolved_project = row.get("project_id") or project_id
            await events_log.emit(
                conn,
                project_id=resolved_project,
                actor_id=getattr(current, "id", None),
                kind="task.created",
                payload={"task_id": str(task_id), "title": title, "type": t_type},
            )
            # Notify assignees directly; Management sees via project audience too.
            audience_users = list(all_assignee_ids) + [str(c) for c in collaborators]
            if reviewer_id:
                audience_users.append(str(reviewer_id))
            audience = audience_users or "project"
            await notifications_service.emit(
                conn,
                project_id=resolved_project,
                kind="task.created",
                payload={"task_id": str(task_id), "title": title},
                audience=audience,
            )

        loaded_assignees = (await _load_assignees(conn, [task_id])).get(str(task_id), [])
        loaded_collaborators = (await _load_collaborators(conn, [task_id])).get(str(task_id), [])

    return _serialize_task(row, loaded_assignees, loaded_collaborators)


@router.patch("/tasks/{task_id}")
async def update_task(
    task_id: UUID,
    body: dict = Body(...),
    current=Depends(optional_current_user),
) -> dict:
    """Partially update a task."""
    fields: list[str] = []
    params: list[Any] = []

    simple_keys = (
        "shot_id",
        "asset_id",
        "type",
        "title",
        "description",
        "assignee_id",
        "reviewer_id",
        "project_id",
        "estimated_hours",
        "actual_hours",
        "due_date",
    )
    for key in simple_keys:
        if key in body:
            if key == "due_date":
                fields.append(f"{key} = ${len(params) + 1}::timestamptz")
                params.append(_coerce_due_date(body[key]))
            else:
                fields.append(f"{key} = ${len(params) + 1}")
                params.append(body[key])
    if "status" in body:
        fields.append(f"status = ${len(params) + 1}")
        params.append(_normalize_status(body["status"]))
    if "metadata" in body:
        fields.append(f"metadata = ${len(params) + 1}::jsonb")
        params.append(json.dumps(body["metadata"]) if body["metadata"] is not None else "{}")

    if not fields and "assignees" not in body and "collaborators" not in body:
        raise HTTPException(status_code=400, detail="No updatable fields provided")

    async with acquire() as conn:
        async with conn.transaction():
            if fields:
                params.append(task_id)
                query = (
                    "UPDATE tasks SET "
                    + ", ".join(fields)
                    + " WHERE id = $"
                    + str(len(params))
                    + """
                    RETURNING id, project_id, shot_id, asset_id, type, title, description,
                              assignee_id, reviewer_id, status, estimated_hours, actual_hours,
                              due_date, metadata, created_at, updated_at
                    """
                )
                try:
                    row = await conn.fetchrow(query, *params)
                except asyncpg.ForeignKeyViolationError as e:
                    raise HTTPException(status_code=400, detail=str(e)) from e
                if not row:
                    raise HTTPException(status_code=404, detail="Task not found")
            else:
                row = await conn.fetchrow(
                    """
                    SELECT id, project_id, shot_id, asset_id, type, title, description,
                           assignee_id, reviewer_id, status, estimated_hours, actual_hours,
                           due_date, metadata, created_at, updated_at
                    FROM tasks WHERE id = $1
                    """,
                    task_id,
                )
                if not row:
                    raise HTTPException(status_code=404, detail="Task not found")

            if "assignees" in body:
                assignees = list({str(a) for a in (body.get("assignees") or [])})
                await conn.execute("DELETE FROM task_assignees WHERE task_id = $1", task_id)
                for uid in assignees:
                    await conn.execute(
                        "INSERT INTO task_assignees(task_id, user_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                        task_id,
                        uid,
                    )
            if "collaborators" in body:
                collaborators = list({str(a) for a in (body.get("collaborators") or [])})
                await conn.execute("DELETE FROM task_collaborators WHERE task_id = $1", task_id)
                for uid in collaborators:
                    await conn.execute(
                        "INSERT INTO task_collaborators(task_id, user_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                        task_id,
                        uid,
                    )

            if "status" in body:
                await events_log.emit(
                    conn,
                    project_id=row["project_id"],
                    actor_id=getattr(current, "id", None),
                    kind="task.status.changed",
                    payload={"task_id": str(task_id), "to": _display_status(row["status"])},
                )

        loaded_assignees = (await _load_assignees(conn, [row["id"]])).get(str(row["id"]), [])
        loaded_collaborators = (await _load_collaborators(conn, [row["id"]])).get(
            str(row["id"]), []
        )

    return _serialize_task(row, loaded_assignees, loaded_collaborators)
