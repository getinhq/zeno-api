"""Tasks API — list, get, create, and update tasks (Postgres)."""
from __future__ import annotations

from typing import Any, Optional
from uuid import UUID

import asyncpg
import json
from fastapi import APIRouter, Body, HTTPException, Path, Query

from app.db import acquire

router = APIRouter(prefix="/api/v1", tags=["tasks"])


@router.get("/tasks")
async def list_tasks(
    project_id: Optional[UUID] = Query(None),
    asset_id: Optional[UUID] = Query(None),
    shot_id: Optional[UUID] = Query(None),
    type: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    assignee_id: Optional[UUID] = Query(None),
) -> list[dict]:
    """
    List tasks with optional filters.

    project_id filter is applied via join tasks -> shots -> sequences -> episodes -> projects
    or via join tasks -> assets -> projects.
    """
    base = """
        SELECT t.id, t.shot_id, t.asset_id, t.type, t.assignee_id,
               t.status, t.estimated_hours, t.actual_hours, t.due_date,
               t.metadata, t.created_at, t.updated_at
        FROM tasks t
    """
    joins: list[str] = []
    conditions: list[str] = []
    params: list[Any] = []

    if project_id:
        joins.append(
            """
            LEFT JOIN shots s ON t.shot_id = s.id
            LEFT JOIN sequences seq ON s.sequence_id = seq.id
            LEFT JOIN episodes e ON seq.episode_id = e.id
            LEFT JOIN projects p1 ON e.project_id = p1.id
            LEFT JOIN assets a ON t.asset_id = a.id
            LEFT JOIN projects p2 ON a.project_id = p2.id
            """
        )
        conditions.append("(p1.id = $1 OR p2.id = $1)")
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
        params.append(status)
    if assignee_id:
        conditions.append(f"t.assignee_id = ${len(params) + 1}")
        params.append(assignee_id)

    query = base + " ".join(joins)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY t.created_at DESC"

    async with acquire() as conn:
        rows = await conn.fetch(query, *params)

    return [
        {
            "id": str(r["id"]),
            "shot_id": str(r["shot_id"]) if r["shot_id"] else None,
            "asset_id": str(r["asset_id"]) if r["asset_id"] else None,
            "type": r["type"],
            "assignee_id": str(r["assignee_id"]) if r["assignee_id"] else None,
            "status": r["status"],
            "estimated_hours": float(r["estimated_hours"]) if r["estimated_hours"] is not None else None,
            "actual_hours": float(r["actual_hours"]) if r["actual_hours"] is not None else None,
            "due_date": r["due_date"].isoformat() if r["due_date"] else None,
            "metadata": dict(r["metadata"]) if r["metadata"] else {},
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        }
        for r in rows
    ]


@router.get("/tasks/{task_id}")
async def get_task(task_id: UUID = Path(...)) -> dict:
    """Get one task by id."""
    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, shot_id, asset_id, type, assignee_id, status,
                   estimated_hours, actual_hours, due_date, metadata,
                   created_at, updated_at
            FROM tasks
            WHERE id = $1
            """,
            task_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")
    return {
        "id": str(row["id"]),
        "shot_id": str(row["shot_id"]) if row["shot_id"] else None,
        "asset_id": str(row["asset_id"]) if row["asset_id"] else None,
        "type": row["type"],
        "assignee_id": str(row["assignee_id"]) if row["assignee_id"] else None,
        "status": row["status"],
        "estimated_hours": float(row["estimated_hours"]) if row["estimated_hours"] is not None else None,
        "actual_hours": float(row["actual_hours"]) if row["actual_hours"] is not None else None,
        "due_date": row["due_date"].isoformat() if row["due_date"] else None,
        "metadata": dict(row["metadata"]) if row["metadata"] else {},
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


@router.post("/tasks")
async def create_task(body: dict = Body(...)) -> dict:
    """
    Create a task. Body may include shot_id and/or asset_id, plus type, optional assignee_id,
    status, estimated_hours, due_date, metadata.
    """
    t_type = body.get("type")
    if not t_type:
        raise HTTPException(status_code=400, detail="type is required")

    shot_id = body.get("shot_id")
    asset_id = body.get("asset_id")
    assignee_id = body.get("assignee_id")
    status = body.get("status", "todo")
    estimated_hours = body.get("estimated_hours")
    due_date = body.get("due_date")
    metadata = body.get("metadata") or {}

    async with acquire() as conn:
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO tasks (
                    shot_id, asset_id, type, assignee_id, status,
                    estimated_hours, due_date, metadata
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7::timestamptz, $8::jsonb)
                RETURNING id, shot_id, asset_id, type, assignee_id, status,
                          estimated_hours, actual_hours, due_date, metadata,
                          created_at, updated_at
                """,
                shot_id,
                asset_id,
                t_type,
                assignee_id,
                status,
                estimated_hours,
                due_date,
                json.dumps(metadata) if metadata else "{}",
            )
        except asyncpg.ForeignKeyViolationError as e:
            raise HTTPException(
                status_code=400, detail="shot_id or asset_id refers to non-existent row"
            ) from e

    return {
        "id": str(row["id"]),
        "shot_id": str(row["shot_id"]) if row["shot_id"] else None,
        "asset_id": str(row["asset_id"]) if row["asset_id"] else None,
        "type": row["type"],
        "assignee_id": str(row["assignee_id"]) if row["assignee_id"] else None,
        "status": row["status"],
        "estimated_hours": float(row["estimated_hours"]) if row["estimated_hours"] is not None else None,
        "actual_hours": float(row["actual_hours"]) if row["actual_hours"] is not None else None,
        "due_date": row["due_date"].isoformat() if row["due_date"] else None,
        "metadata": dict(row["metadata"]) if row["metadata"] else {},
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


@router.patch("/tasks/{task_id}")
async def update_task(task_id: UUID, body: dict = Body(...)) -> dict:
    """Partially update a task."""
    fields: list[str] = []
    params: list[Any] = []

    for key in ("shot_id", "asset_id", "type", "assignee_id", "status", "estimated_hours", "actual_hours", "due_date"):
        if key in body:
            if key == "due_date":
                fields.append(f"{key} = ${len(params) + 1}::timestamptz")
            else:
                fields.append(f"{key} = ${len(params) + 1}")
            params.append(body[key])
    if "metadata" in body:
        fields.append(f"metadata = ${len(params) + 1}::jsonb")
        params.append(json.dumps(body["metadata"]) if body["metadata"] is not None else "{}")

    if not fields:
        raise HTTPException(status_code=400, detail="No updatable fields provided")

    params.append(task_id)
    query = (
        "UPDATE tasks SET "
        + ", ".join(fields)
        + " WHERE id = $"
        + str(len(params))
        + """
        RETURNING id, shot_id, asset_id, type, assignee_id, status,
                  estimated_hours, actual_hours, due_date, metadata,
                  created_at, updated_at
        """
    )

    async with acquire() as conn:
        try:
            row = await conn.fetchrow(query, *params)
        except asyncpg.ForeignKeyViolationError as e:
            raise HTTPException(
                status_code=400, detail="shot_id or asset_id refers to non-existent row"
            ) from e

    if not row:
        raise HTTPException(status_code=404, detail="Task not found")

    return {
        "id": str(row["id"]),
        "shot_id": str(row["shot_id"]) if row["shot_id"] else None,
        "asset_id": str(row["asset_id"]) if row["asset_id"] else None,
        "type": row["type"],
        "assignee_id": str(row["assignee_id"]) if row["assignee_id"] else None,
        "status": row["status"],
        "estimated_hours": float(row["estimated_hours"]) if row["estimated_hours"] is not None else None,
        "actual_hours": float(row["actual_hours"]) if row["actual_hours"] is not None else None,
        "due_date": row["due_date"].isoformat() if row["due_date"] else None,
        "metadata": dict(row["metadata"]) if row["metadata"] else {},
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }

