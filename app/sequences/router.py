"""Sequences API — list, get, create, update (Postgres)."""
from __future__ import annotations

import json
from typing import Any, Optional
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Body, HTTPException, Path, Query

from app.db import acquire

router = APIRouter(prefix="/api/v1", tags=["sequences"])
ALLOWED_STAGES = ("Animatics", "Layout", "Animation", "Lighting", "Comp")

def _norm_metadata(value: Any) -> dict:
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


@router.get("/episodes/{episode_id}/sequences")
async def list_sequences_for_episode(
    episode_id: UUID,
    code: Optional[str] = Query(None, description="Optional sequence code filter"),
    stage: Optional[str] = Query(None, description="Optional stage filter"),
    search: Optional[str] = Query(None, description="Optional text search on code or name"),
) -> list[dict]:
    """List sequences for an episode."""
    query = """
        SELECT id, episode_id, name, code, stage, start_frame, end_frame, metadata, created_at, updated_at
        FROM sequences
        WHERE episode_id = $1
    """
    params: list[Any] = [episode_id]
    if code:
        query += " AND code = $2"
        params.append(code)
    if stage:
        idx = len(params) + 1
        query += f" AND stage = ${idx}"
        params.append(stage)
    if search:
        idx = len(params) + 1
        query += f" AND (code ILIKE ${idx} OR name ILIKE ${idx})"
        params.append(f"%{search}%")
    query += " ORDER BY code"

    async with acquire() as conn:
        rows = await conn.fetch(query, *params)
    return [
        {
            "id": str(r["id"]),
            "episode_id": str(r["episode_id"]),
            "name": r["name"],
            "code": r["code"],
            "stage": r["stage"],
            "start_frame": r["start_frame"],
            "end_frame": r["end_frame"],
            "metadata": _norm_metadata(r["metadata"]),
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        }
        for r in rows
    ]


@router.get("/projects/{project_id}/sequences")
async def list_sequences_for_project(
    project_id: UUID,
    episode_ids: Optional[list[UUID]] = Query(None, description="Optional episode_id filters"),
    stage: Optional[str] = Query(None, description="Optional stage filter"),
    search: Optional[str] = Query(None, description="Optional text search on code or name"),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> list[dict]:
    """List sequences for a project with optional filtering."""
    query = """
        SELECT s.id, s.episode_id, s.name, s.code, s.stage, s.start_frame, s.end_frame, s.metadata, s.created_at, s.updated_at
        FROM sequences s
        JOIN episodes e ON e.id = s.episode_id
        WHERE e.project_id = $1
    """
    params: list[Any] = [project_id]
    if episode_ids:
        idx = len(params) + 1
        query += f" AND s.episode_id = ANY(${idx}::uuid[])"
        params.append([str(v) for v in episode_ids])
    if stage:
        idx = len(params) + 1
        query += f" AND s.stage = ${idx}"
        params.append(stage)
    if search:
        idx = len(params) + 1
        query += f" AND (s.code ILIKE ${idx} OR s.name ILIKE ${idx})"
        params.append(f"%{search}%")
    query += " ORDER BY s.code LIMIT $" + str(len(params) + 1) + " OFFSET $" + str(len(params) + 2)
    params.extend([limit, offset])

    async with acquire() as conn:
        rows = await conn.fetch(query, *params)
    return [
        {
            "id": str(r["id"]),
            "episode_id": str(r["episode_id"]),
            "name": r["name"],
            "code": r["code"],
            "stage": r["stage"],
            "start_frame": r["start_frame"],
            "end_frame": r["end_frame"],
            "metadata": _norm_metadata(r["metadata"]),
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        }
        for r in rows
    ]


@router.get("/sequences/{sequence_id}")
async def get_sequence(sequence_id: UUID = Path(...)) -> dict:
    """Get one sequence by id."""
    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, episode_id, name, code, stage, start_frame, end_frame, metadata, created_at, updated_at
            FROM sequences
            WHERE id = $1
            """,
            sequence_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Sequence not found")
    return {
        "id": str(row["id"]),
        "episode_id": str(row["episode_id"]),
        "name": row["name"],
        "code": row["code"],
        "stage": row["stage"],
        "start_frame": row["start_frame"],
        "end_frame": row["end_frame"],
        "metadata": _norm_metadata(row["metadata"]),
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


@router.post("/episodes/{episode_id}/sequences")
async def create_sequence(episode_id: UUID, body: dict = Body(...)) -> dict:
    """Create a sequence under an episode. Expects JSON: name, code; optional metadata."""
    name = body.get("name")
    code = body.get("code")
    stage = body.get("stage", "Layout")
    if not name or not code:
        raise HTTPException(status_code=400, detail="name and code are required")
    if stage not in ALLOWED_STAGES:
        raise HTTPException(status_code=400, detail=f"stage must be one of: {', '.join(ALLOWED_STAGES)}")
    metadata = body.get("metadata") or {}
    start_frame = body.get("start_frame")
    end_frame = body.get("end_frame")

    async with acquire() as conn:
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO sequences (episode_id, name, code, stage, start_frame, end_frame, metadata)
                VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
                RETURNING id, episode_id, name, code, stage, start_frame, end_frame, metadata, created_at, updated_at
                """,
                episode_id,
                name,
                code,
                stage,
                start_frame,
                end_frame,
                json.dumps(metadata) if metadata else "{}",
            )
        except asyncpg.ForeignKeyViolationError as e:
            raise HTTPException(status_code=404, detail="Episode not found") from e
        except asyncpg.UniqueViolationError:
            raise HTTPException(status_code=409, detail="Sequence code must be unique per episode")

    return {
        "id": str(row["id"]),
        "episode_id": str(row["episode_id"]),
        "name": row["name"],
        "code": row["code"],
        "stage": row["stage"],
        "start_frame": row["start_frame"],
        "end_frame": row["end_frame"],
        "metadata": _norm_metadata(row["metadata"]),
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


@router.patch("/sequences/{sequence_id}")
async def update_sequence(sequence_id: UUID, body: dict = Body(...)) -> dict:
    """Partially update a sequence."""
    fields: list[str] = []
    params: list[Any] = []
    if "stage" in body and body["stage"] not in ALLOWED_STAGES:
        raise HTTPException(status_code=400, detail=f"stage must be one of: {', '.join(ALLOWED_STAGES)}")

    for key in ("name", "code", "stage", "start_frame", "end_frame", "metadata"):
        if key in body:
            if key == "metadata":
                fields.append(f"{key} = ${len(params) + 1}::jsonb")
                params.append(json.dumps(body[key]) if body[key] is not None else "{}")
            else:
                fields.append(f"{key} = ${len(params) + 1}")
                params.append(body[key])
    if not fields:
        raise HTTPException(status_code=400, detail="No updatable fields provided")
    params.append(sequence_id)
    query = (
        "UPDATE sequences SET "
        + ", ".join(fields)
        + " WHERE id = $"
        + str(len(params))
        + " RETURNING id, episode_id, name, code, stage, start_frame, end_frame, metadata, created_at, updated_at"
    )
    async with acquire() as conn:
        try:
            row = await conn.fetchrow(query, *params)
        except asyncpg.UniqueViolationError:
            raise HTTPException(status_code=409, detail="Sequence code must be unique per episode")
    if not row:
        raise HTTPException(status_code=404, detail="Sequence not found")
    return {
        "id": str(row["id"]),
        "episode_id": str(row["episode_id"]),
        "name": row["name"],
        "code": row["code"],
        "stage": row["stage"],
        "start_frame": row["start_frame"],
        "end_frame": row["end_frame"],
        "metadata": _norm_metadata(row["metadata"]),
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }
