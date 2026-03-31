"""Projects API — list, create, update, and soft-delete projects (Postgres)."""
from __future__ import annotations

import json
from typing import Any, Optional
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Body, HTTPException, Query

from app.db import acquire

router = APIRouter(prefix="/api/v1/projects", tags=["projects"])


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


@router.get("")
async def list_projects(
    status: Optional[str] = Query(None, description="Optional status filter"),
    code: Optional[str] = Query(None, description="Optional exact code filter"),
) -> list[dict]:
    """List projects (id, name, code, status), optionally filtered by status/code."""
    query = "SELECT id, name, code, status, created_at FROM projects"
    conditions: list[str] = []
    params: list[Any] = []
    if status:
        conditions.append("status = $1")
        params.append(status)
    if code:
        conditions.append(f"{'status = $1 AND ' if status else ''}code = ${len(params) + 1}")
        params.append(code)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY name"

    async with acquire() as conn:
        rows = await conn.fetch(query, *params)
    return [
        {
            "id": str(r["id"]),
            "name": r["name"],
            "code": r["code"],
            "status": r["status"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]


@router.get("/{project_id}")
async def get_project(project_id: UUID) -> dict:
    """Get one project by id."""
    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, name, code, status, start_date, end_date, metadata, created_at, updated_at
            FROM projects
            WHERE id = $1
            """,
            project_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Project not found")
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "code": row["code"],
        "status": row["status"],
        "start_date": row["start_date"].isoformat() if row["start_date"] else None,
        "end_date": row["end_date"].isoformat() if row["end_date"] else None,
        "metadata": _json_metadata(row["metadata"]),
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


@router.post("")
async def create_project(body: dict = Body(...)) -> dict:
    """Create a project. Expects JSON: name, code; optional: status, start_date, end_date, metadata."""
    name = body.get("name")
    code = body.get("code")
    if not name or not code:
        raise HTTPException(status_code=400, detail="name and code are required")
    status = body.get("status", "active")
    start_date = body.get("start_date")
    end_date = body.get("end_date")
    metadata = body.get("metadata") or {}

    async with acquire() as conn:
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO projects (name, code, status, start_date, end_date, metadata)
                VALUES ($1, $2, $3, $4::date, $5::date, $6::jsonb)
                RETURNING id, name, code, status, created_at
                """,
                name,
                code,
                status,
                start_date,
                end_date,
                json.dumps(metadata) if metadata else "{}",
            )
        except asyncpg.UniqueViolationError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e

    return {
        "id": str(row["id"]),
        "name": row["name"],
        "code": row["code"],
        "status": row["status"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


@router.patch("/{project_id}")
async def update_project(project_id: UUID, body: dict = Body(...)) -> dict:
    """Partially update a project."""
    fields: list[str] = []
    params: list[Any] = []
    allowed_fields = ("name", "status", "start_date", "end_date", "metadata")

    for key in allowed_fields:
        if key in body:
            if key in ("start_date", "end_date"):
                fields.append(f"{key} = ${len(params) + 1}::date")
            elif key == "metadata":
                fields.append(f"{key} = ${len(params) + 1}::jsonb")
                params.append(json.dumps(body[key]) if body[key] is not None else "{}")
                continue
            else:
                fields.append(f"{key} = ${len(params) + 1}")
            params.append(body[key])

    if not fields:
        raise HTTPException(status_code=400, detail="No updatable fields provided")

    params.append(project_id)
    query = (
        "UPDATE projects SET "
        + ", ".join(fields)
        + " WHERE id = $"
        + str(len(params))
        + " RETURNING id, name, code, status, start_date, end_date, metadata, created_at, updated_at"
    )

    async with acquire() as conn:
        try:
            row = await conn.fetchrow(query, *params)
        except asyncpg.UniqueViolationError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e

    if not row:
        raise HTTPException(status_code=404, detail="Project not found")

    return {
        "id": str(row["id"]),
        "name": row["name"],
        "code": row["code"],
        "status": row["status"],
        "start_date": row["start_date"].isoformat() if row["start_date"] else None,
        "end_date": row["end_date"].isoformat() if row["end_date"] else None,
        "metadata": _json_metadata(row["metadata"]),
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


@router.delete("/{project_id}")
async def delete_project(project_id: UUID) -> dict:
    """Soft-delete a project by setting status='archived'."""
    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE projects
            SET status = 'archived'
            WHERE id = $1
            RETURNING id, name, code, status
            """,
            project_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Project not found")
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "code": row["code"],
        "status": row["status"],
    }

