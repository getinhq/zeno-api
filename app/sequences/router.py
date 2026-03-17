"""Sequences API — list, get, create, update (Postgres)."""
from __future__ import annotations

import json
from typing import Any, Optional
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Body, HTTPException, Path, Query

from app.db import acquire

router = APIRouter(prefix="/api/v1", tags=["sequences"])


@router.get("/episodes/{episode_id}/sequences")
async def list_sequences_for_episode(
    episode_id: UUID,
    code: Optional[str] = Query(None, description="Optional sequence code filter"),
) -> list[dict]:
    """List sequences for an episode."""
    query = """
        SELECT id, episode_id, name, code, metadata, created_at, updated_at
        FROM sequences
        WHERE episode_id = $1
    """
    params: list[Any] = [episode_id]
    if code:
        query += " AND code = $2"
        params.append(code)
    query += " ORDER BY code"

    async with acquire() as conn:
        rows = await conn.fetch(query, *params)
    return [
        {
            "id": str(r["id"]),
            "episode_id": str(r["episode_id"]),
            "name": r["name"],
            "code": r["code"],
            "metadata": dict(r["metadata"]) if r["metadata"] else {},
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
            SELECT id, episode_id, name, code, metadata, created_at, updated_at
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
        "metadata": dict(row["metadata"]) if row["metadata"] else {},
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


@router.post("/episodes/{episode_id}/sequences")
async def create_sequence(episode_id: UUID, body: dict = Body(...)) -> dict:
    """Create a sequence under an episode. Expects JSON: name, code; optional metadata."""
    name = body.get("name")
    code = body.get("code")
    if not name or not code:
        raise HTTPException(status_code=400, detail="name and code are required")
    metadata = body.get("metadata") or {}

    async with acquire() as conn:
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO sequences (episode_id, name, code, metadata)
                VALUES ($1, $2, $3, $4::jsonb)
                RETURNING id, episode_id, name, code, metadata, created_at, updated_at
                """,
                episode_id,
                name,
                code,
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
        "metadata": dict(row["metadata"]) if row["metadata"] else {},
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


@router.patch("/sequences/{sequence_id}")
async def update_sequence(sequence_id: UUID, body: dict = Body(...)) -> dict:
    """Partially update a sequence."""
    fields: list[str] = []
    params: list[Any] = []
    for key in ("name", "code", "metadata"):
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
        + " RETURNING id, episode_id, name, code, metadata, created_at, updated_at"
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
        "metadata": dict(row["metadata"]) if row["metadata"] else {},
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }
