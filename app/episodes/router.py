"""Episodes API — list, get, create, update (Postgres)."""
from __future__ import annotations

import json
from typing import Any, Optional
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Body, HTTPException, Path, Query

from app.db import acquire

router = APIRouter(prefix="/api/v1", tags=["episodes"])


@router.get("/projects/{project_id}/episodes")
async def list_episodes_for_project(
    project_id: UUID,
    code: Optional[str] = Query(None, description="Optional episode code filter"),
) -> list[dict]:
    """List episodes for a project."""
    query = """
        SELECT id, project_id, episode_number, title, code, status, air_date, metadata, created_at, updated_at
        FROM episodes
        WHERE project_id = $1
    """
    params: list[Any] = [project_id]
    if code:
        query += " AND code = $2"
        params.append(code)
    query += " ORDER BY episode_number, code"

    async with acquire() as conn:
        rows = await conn.fetch(query, *params)
    return [
        {
            "id": str(r["id"]),
            "project_id": str(r["project_id"]),
            "episode_number": r["episode_number"],
            "title": r["title"],
            "code": r["code"],
            "status": r["status"],
            "air_date": r["air_date"].isoformat() if r["air_date"] else None,
            "metadata": dict(r["metadata"]) if r["metadata"] else {},
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        }
        for r in rows
    ]


@router.get("/episodes/{episode_id}")
async def get_episode(episode_id: UUID = Path(...)) -> dict:
    """Get one episode by id."""
    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, project_id, episode_number, title, code, status, air_date, metadata, created_at, updated_at
            FROM episodes
            WHERE id = $1
            """,
            episode_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Episode not found")
    return {
        "id": str(row["id"]),
        "project_id": str(row["project_id"]),
        "episode_number": row["episode_number"],
        "title": row["title"],
        "code": row["code"],
        "status": row["status"],
        "air_date": row["air_date"].isoformat() if row["air_date"] else None,
        "metadata": dict(row["metadata"]) if row["metadata"] else {},
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


@router.post("/projects/{project_id}/episodes")
async def create_episode(project_id: UUID, body: dict = Body(...)) -> dict:
    """Create an episode under a project. Expects JSON: episode_number, code; optional: title, status, air_date, metadata."""
    episode_number = body.get("episode_number")
    code = body.get("code")
    if code is None or code == "":
        raise HTTPException(status_code=400, detail="code is required")
    if episode_number is None:
        raise HTTPException(status_code=400, detail="episode_number is required")
    title = body.get("title")
    status = body.get("status", "in_production")
    air_date = body.get("air_date")
    metadata = body.get("metadata") or {}

    async with acquire() as conn:
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO episodes (project_id, episode_number, title, code, status, air_date, metadata)
                VALUES ($1, $2, $3, $4, $5, $6::date, $7::jsonb)
                RETURNING id, project_id, episode_number, title, code, status, air_date, metadata, created_at, updated_at
                """,
                project_id,
                episode_number,
                title,
                code,
                status,
                air_date,
                json.dumps(metadata) if metadata else "{}",
            )
        except asyncpg.ForeignKeyViolationError as e:
            raise HTTPException(status_code=404, detail="Project not found") from e
        except asyncpg.UniqueViolationError:
            raise HTTPException(status_code=409, detail="Episode code must be unique per project")

    return {
        "id": str(row["id"]),
        "project_id": str(row["project_id"]),
        "episode_number": row["episode_number"],
        "title": row["title"],
        "code": row["code"],
        "status": row["status"],
        "air_date": row["air_date"].isoformat() if row["air_date"] else None,
        "metadata": dict(row["metadata"]) if row["metadata"] else {},
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


@router.patch("/episodes/{episode_id}")
async def update_episode(episode_id: UUID, body: dict = Body(...)) -> dict:
    """Partially update an episode."""
    fields: list[str] = []
    params: list[Any] = []
    allowed = ("episode_number", "title", "code", "status", "air_date", "metadata")
    for key in allowed:
        if key in body:
            if key == "air_date":
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
    params.append(episode_id)
    query = (
        "UPDATE episodes SET "
        + ", ".join(fields)
        + " WHERE id = $"
        + str(len(params))
        + " RETURNING id, project_id, episode_number, title, code, status, air_date, metadata, created_at, updated_at"
    )
    async with acquire() as conn:
        try:
            row = await conn.fetchrow(query, *params)
        except asyncpg.UniqueViolationError:
            raise HTTPException(status_code=409, detail="Episode code must be unique per project")
    if not row:
        raise HTTPException(status_code=404, detail="Episode not found")
    return {
        "id": str(row["id"]),
        "project_id": str(row["project_id"]),
        "episode_number": row["episode_number"],
        "title": row["title"],
        "code": row["code"],
        "status": row["status"],
        "air_date": row["air_date"].isoformat() if row["air_date"] else None,
        "metadata": dict(row["metadata"]) if row["metadata"] else {},
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }
