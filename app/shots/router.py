"""Shots API — list, get, create, and update shots (Postgres)."""
from __future__ import annotations

import json
from typing import Any, Optional
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Body, HTTPException, Path, Query

from app.db import acquire

router = APIRouter(prefix="/api/v1", tags=["shots"])
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


@router.get("/sequences/{sequence_id}/shots")
async def list_shots_for_sequence(
    sequence_id: UUID,
    status: Optional[str] = Query(None, description="Optional status filter"),
    shot_code: Optional[str] = Query(None, description="Optional shot code filter"),
    stage: Optional[str] = Query(None, description="Optional stage filter"),
    search: Optional[str] = Query(None, description="Optional text search by shot_code"),
) -> list[dict]:
    """List shots for a sequence, optionally filtered by status and shot_code."""
    query = """
        SELECT id, sequence_id, shot_code, stage, frame_start, frame_end,
               handle_in, handle_out, status, metadata, created_at, updated_at
        FROM shots
        WHERE sequence_id = $1
    """
    params: list[Any] = [sequence_id]
    if status:
        query += " AND status = $2"
        params.append(status)
    if shot_code:
        idx = len(params) + 1
        query += f" AND shot_code = ${idx}"
        params.append(shot_code)
    if stage:
        idx = len(params) + 1
        query += f" AND stage = ${idx}"
        params.append(stage)
    if search:
        idx = len(params) + 1
        query += f" AND shot_code ILIKE ${idx}"
        params.append(f"%{search}%")
    query += " ORDER BY shot_code"

    async with acquire() as conn:
        rows = await conn.fetch(query, *params)
    return [
        {
            "id": str(r["id"]),
            "sequence_id": str(r["sequence_id"]),
            "shot_code": r["shot_code"],
            "stage": r["stage"],
            "frame_start": r["frame_start"],
            "frame_end": r["frame_end"],
            "handle_in": r["handle_in"],
            "handle_out": r["handle_out"],
            "status": r["status"],
            "metadata": _norm_metadata(r["metadata"]),
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        }
        for r in rows
    ]


@router.get("/projects/{project_id}/shots")
async def list_shots_for_project(
    project_id: UUID,
    episode_ids: Optional[list[UUID]] = Query(None, description="Optional episode_id filters"),
    sequence_ids: Optional[list[UUID]] = Query(None, description="Optional sequence_id filters"),
    stage: Optional[str] = Query(None, description="Optional stage filter"),
    search: Optional[str] = Query(None, description="Optional text search by shot_code"),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> list[dict]:
    """List shots for a project with optional filtering and pagination."""
    query = """
        SELECT sh.id, sh.sequence_id, sq.episode_id, sh.shot_code, sh.stage,
               sh.frame_start, sh.frame_end, sh.handle_in, sh.handle_out,
               sh.status, sh.metadata, sh.created_at, sh.updated_at
        FROM shots sh
        JOIN sequences sq ON sq.id = sh.sequence_id
        JOIN episodes ep ON ep.id = sq.episode_id
        WHERE ep.project_id = $1
    """
    params: list[Any] = [project_id]
    if episode_ids:
        idx = len(params) + 1
        query += f" AND sq.episode_id = ANY(${idx}::uuid[])"
        params.append([str(v) for v in episode_ids])
    if sequence_ids:
        idx = len(params) + 1
        query += f" AND sh.sequence_id = ANY(${idx}::uuid[])"
        params.append([str(v) for v in sequence_ids])
    if stage:
        idx = len(params) + 1
        query += f" AND sh.stage = ${idx}"
        params.append(stage)
    if search:
        idx = len(params) + 1
        query += f" AND sh.shot_code ILIKE ${idx}"
        params.append(f"%{search}%")
    query += " ORDER BY sh.shot_code LIMIT $" + str(len(params) + 1) + " OFFSET $" + str(len(params) + 2)
    params.extend([limit, offset])

    async with acquire() as conn:
        rows = await conn.fetch(query, *params)
    return [
        {
            "id": str(r["id"]),
            "sequence_id": str(r["sequence_id"]),
            "episode_id": str(r["episode_id"]),
            "shot_code": r["shot_code"],
            "stage": r["stage"],
            "frame_start": r["frame_start"],
            "frame_end": r["frame_end"],
            "handle_in": r["handle_in"],
            "handle_out": r["handle_out"],
            "status": r["status"],
            "metadata": _norm_metadata(r["metadata"]),
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        }
        for r in rows
    ]


@router.get("/shots/{shot_id}")
async def get_shot(shot_id: UUID = Path(...)) -> dict:
    """Get one shot by id."""
    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, sequence_id, shot_code, stage, frame_start, frame_end,
                   handle_in, handle_out, status, metadata, created_at, updated_at
            FROM shots
            WHERE id = $1
            """,
            shot_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Shot not found")
    return {
        "id": str(row["id"]),
        "sequence_id": str(row["sequence_id"]),
        "shot_code": row["shot_code"],
        "stage": row["stage"],
        "frame_start": row["frame_start"],
        "frame_end": row["frame_end"],
        "handle_in": row["handle_in"],
        "handle_out": row["handle_out"],
        "status": row["status"],
        "metadata": _norm_metadata(row["metadata"]),
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


@router.post("/sequences/{sequence_id}/shots")
async def create_shot(sequence_id: UUID, body: dict = Body(...)) -> dict:
    """Create a shot in a sequence."""
    shot_code = body.get("shot_code")
    if not shot_code:
        raise HTTPException(status_code=400, detail="shot_code is required")

    frame_start = body.get("frame_start")
    frame_end = body.get("frame_end")
    handle_in = body.get("handle_in", 0)
    handle_out = body.get("handle_out", 0)
    status = body.get("status", "pending")
    stage = body.get("stage", "Layout")
    metadata = body.get("metadata") or {}
    if stage not in ALLOWED_STAGES:
        raise HTTPException(status_code=400, detail=f"stage must be one of: {', '.join(ALLOWED_STAGES)}")

    async with acquire() as conn:
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO shots (
                    sequence_id, shot_code, stage, frame_start, frame_end,
                    handle_in, handle_out, status, metadata
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
                RETURNING id, sequence_id, shot_code, stage, frame_start, frame_end,
                          handle_in, handle_out, status, metadata, created_at, updated_at
                """,
                sequence_id,
                shot_code,
                stage,
                frame_start,
                frame_end,
                handle_in,
                handle_out,
                status,
                json.dumps(metadata) if metadata else "{}",
            )
        except asyncpg.ForeignKeyViolationError as e:
            raise HTTPException(status_code=404, detail="Sequence not found") from e
        except asyncpg.UniqueViolationError:
            raise HTTPException(
                status_code=409,
                detail="shot_code must be unique per sequence",
            )

    return {
        "id": str(row["id"]),
        "sequence_id": str(row["sequence_id"]),
        "shot_code": row["shot_code"],
        "stage": row["stage"],
        "frame_start": row["frame_start"],
        "frame_end": row["frame_end"],
        "handle_in": row["handle_in"],
        "handle_out": row["handle_out"],
        "status": row["status"],
        "metadata": _norm_metadata(row["metadata"]),
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


@router.patch("/shots/{shot_id}")
async def update_shot(shot_id: UUID, body: dict = Body(...)) -> dict:
    """Partially update a shot."""
    fields: list[str] = []
    params: list[Any] = []

    if "stage" in body and body["stage"] not in ALLOWED_STAGES:
        raise HTTPException(status_code=400, detail=f"stage must be one of: {', '.join(ALLOWED_STAGES)}")

    for key in ("frame_start", "frame_end", "handle_in", "handle_out", "status", "stage"):
        if key in body:
            fields.append(f"{key} = ${len(params) + 1}")
            params.append(body[key])
    if "metadata" in body:
        fields.append(f"metadata = ${len(params) + 1}::jsonb")
        params.append(json.dumps(body["metadata"]) if body["metadata"] is not None else "{}")

    if not fields:
        raise HTTPException(status_code=400, detail="No updatable fields provided")

    params.append(shot_id)
    query = (
        "UPDATE shots SET "
        + ", ".join(fields)
        + " WHERE id = $"
        + str(len(params))
        + """
        RETURNING id, sequence_id, shot_code, stage, frame_start, frame_end,
                  handle_in, handle_out, status, metadata, created_at, updated_at
        """
    )

    async with acquire() as conn:
        row = await conn.fetchrow(query, *params)

    if not row:
        raise HTTPException(status_code=404, detail="Shot not found")

    return {
        "id": str(row["id"]),
        "sequence_id": str(row["sequence_id"]),
        "shot_code": row["shot_code"],
        "stage": row["stage"],
        "frame_start": row["frame_start"],
        "frame_end": row["frame_end"],
        "handle_in": row["handle_in"],
        "handle_out": row["handle_out"],
        "status": row["status"],
        "metadata": _norm_metadata(row["metadata"]),
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }

