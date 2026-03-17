"""Assets API — list, get, create, and update assets (Postgres)."""
from __future__ import annotations

import json
from typing import Any, Optional
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Body, HTTPException, Path, Query

from app.db import acquire

router = APIRouter(prefix="/api/v1", tags=["assets"])

def _norm_metadata(value: Any) -> dict:
    """Normalize asyncpg json/jsonb values to a dict."""
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
    # asyncpg may return list/tuple for some codecs; treat non-dict as empty for now
    return {}


@router.get("/projects/{project_id}/assets")
async def list_assets_for_project(
    project_id: UUID,
    type: Optional[str] = Query(None, alias="type"),
    code: Optional[str] = Query(None, description="Optional exact asset code filter"),
) -> list[dict]:
    """List assets for a project, optionally filtered by type/code."""
    query = """
        SELECT id, project_id, type, name, code, metadata, created_at, updated_at
        FROM assets
        WHERE project_id = $1
    """
    params: list[Any] = [project_id]
    if type:
        query += " AND type = $2"
        params.append(type)
    if code:
        idx = len(params) + 1
        query += f" AND code = ${idx}"
        params.append(code)
    query += " ORDER BY code"

    async with acquire() as conn:
        rows = await conn.fetch(query, *params)
    return [
        {
            "id": str(r["id"]),
            "project_id": str(r["project_id"]),
            "type": r["type"],
            "name": r["name"],
            "code": r["code"],
            "metadata": _norm_metadata(r["metadata"]),
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        }
        for r in rows
    ]


@router.get("/assets/{asset_id}")
async def get_asset(asset_id: UUID = Path(...)) -> dict:
    """Get one asset by id."""
    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, project_id, type, name, code, metadata, created_at, updated_at
            FROM assets
            WHERE id = $1
            """,
            asset_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Asset not found")
    return {
        "id": str(row["id"]),
        "project_id": str(row["project_id"]),
        "type": row["type"],
        "name": row["name"],
        "code": row["code"],
        "metadata": _norm_metadata(row["metadata"]),
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


@router.post("/projects/{project_id}/assets")
async def create_asset(project_id: UUID, body: dict = Body(...)) -> dict:
    """Create an asset under a project. Expects JSON: type, name, code; optional metadata."""
    a_type = body.get("type")
    name = body.get("name")
    code = body.get("code")
    if not a_type or not name or not code:
        raise HTTPException(status_code=400, detail="type, name, and code are required")
    metadata = body.get("metadata") or {}

    async with acquire() as conn:
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO assets (project_id, type, name, code, metadata)
                VALUES ($1, $2, $3, $4, $5::jsonb)
                RETURNING id, project_id, type, name, code, metadata, created_at, updated_at
                """,
                project_id,
                a_type,
                name,
                code,
                json.dumps(metadata) if metadata else "{}",
            )
        except asyncpg.ForeignKeyViolationError as e:
            raise HTTPException(status_code=404, detail="Project not found") from e
        except asyncpg.UniqueViolationError as e:
            raise HTTPException(
                status_code=409,
                detail="Asset code must be unique per project",
            ) from e

    return {
        "id": str(row["id"]),
        "project_id": str(row["project_id"]),
        "type": row["type"],
        "name": row["name"],
        "code": row["code"],
        "metadata": _norm_metadata(row["metadata"]),
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


@router.patch("/assets/{asset_id}")
async def update_asset(asset_id: UUID, body: dict = Body(...)) -> dict:
    """Partially update an asset."""
    fields: list[str] = []
    params: list[Any] = []

    if "type" in body:
        fields.append(f"type = ${len(params) + 1}")
        params.append(body["type"])
    if "name" in body:
        fields.append(f"name = ${len(params) + 1}")
        params.append(body["name"])
    if "metadata" in body:
        fields.append(f"metadata = ${len(params) + 1}::jsonb")
        params.append(json.dumps(body["metadata"]) if body["metadata"] is not None else "{}")

    if not fields:
        raise HTTPException(status_code=400, detail="No updatable fields provided")

    params.append(asset_id)
    query = (
        "UPDATE assets SET "
        + ", ".join(fields)
        + " WHERE id = $"
        + str(len(params))
        + """
        RETURNING id, project_id, type, name, code, metadata, created_at, updated_at
        """
    )

    async with acquire() as conn:
        try:
            row = await conn.fetchrow(query, *params)
        except asyncpg.UniqueViolationError as e:
            raise HTTPException(
                status_code=409,
                detail="Asset code must be unique per project",
            ) from e

    if not row:
        raise HTTPException(status_code=404, detail="Asset not found")

    return {
        "id": str(row["id"]),
        "project_id": str(row["project_id"]),
        "type": row["type"],
        "name": row["name"],
        "code": row["code"],
        "metadata": _norm_metadata(row["metadata"]),
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }

