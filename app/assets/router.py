"""Assets API — list, get, create, and update assets (Postgres)."""
from __future__ import annotations

import json
from typing import Any, Optional
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Body, HTTPException, Path, Query

from app.db import acquire

router = APIRouter(prefix="/api/v1", tags=["assets"])

ALLOWED_ASSET_PIPELINE_STAGES = frozenset(
    ("modelling", "texturing", "rigging", "lookdev"),
)

ALLOWED_PIPELINE_STAGE_STATUS = frozenset(
    ("not_started", "in_progress", "review", "done", "approved", "na"),
)

_PIPELINE_ORDER = ("modelling", "texturing", "rigging", "lookdev")


def _norm_pipeline_stages(value: Any) -> list[str]:
    """Normalize to an ordered subset of allowed pipeline stage labels."""
    order = ("modelling", "texturing", "rigging", "lookdev")
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        raw = {str(x).strip().lower() for x in value}
        return [s for s in order if s in raw]
    return []


def _norm_pipeline_stage_status(value: Any) -> dict[str, str]:
    """Per asset-pipeline-stage status: not_started | in_progress | review | done | approved | na."""
    out: dict[str, str] = {}
    if isinstance(value, dict):
        for k, v in value.items():
            ks = str(k).strip().lower()
            if ks not in ALLOWED_ASSET_PIPELINE_STAGES:
                continue
            vs = str(v).strip().lower() if v is not None else "not_started"
            if vs not in ALLOWED_PIPELINE_STAGE_STATUS:
                vs = "not_started"
            out[ks] = vs
    for s in _PIPELINE_ORDER:
        if s not in out:
            out[s] = "not_started"
    return out


def _stages_from_status(status: dict[str, str]) -> list[str]:
    return [s for s in _PIPELINE_ORDER if status.get(s) not in (None, "", "na")]


def _status_from_legacy_stages(legacy: list[str]) -> dict[str, str]:
    leg = set(legacy)
    return {s: ("not_started" if s in leg else "na") for s in _PIPELINE_ORDER}


def _row_pipeline_stage_status(row: Any) -> dict[str, str]:
    raw = row["pipeline_stage_status"] if "pipeline_stage_status" in row else None
    if raw is None or (isinstance(raw, dict) and len(raw) == 0):
        return _status_from_legacy_stages(_norm_pipeline_stages(row["pipeline_stages"]))
    return _norm_pipeline_stage_status(raw)


def _row_pipeline_stages(row: Any) -> list[str]:
    raw = row["pipeline_stage_status"] if "pipeline_stage_status" in row else None
    if raw is None or (isinstance(raw, dict) and len(raw) == 0):
        return _norm_pipeline_stages(row["pipeline_stages"])
    return _stages_from_status(_norm_pipeline_stage_status(raw))


def _asset_row_dict(row: Any) -> dict:
    return {
        "id": str(row["id"]),
        "project_id": str(row["project_id"]),
        "type": row["type"],
        "name": row["name"],
        "code": row["code"],
        "pipeline_stages": _row_pipeline_stages(row),
        "pipeline_stage_status": _row_pipeline_stage_status(row),
        "metadata": _norm_metadata(row["metadata"]),
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


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
        SELECT id, project_id, type, name, code, pipeline_stages, pipeline_stage_status, metadata,
               created_at, updated_at
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
    return [_asset_row_dict(r) for r in rows]


@router.get("/assets/{asset_id}")
async def get_asset(asset_id: UUID = Path(...)) -> dict:
    """Get one asset by id."""
    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, project_id, type, name, code, pipeline_stages, pipeline_stage_status, metadata,
                   created_at, updated_at
            FROM assets
            WHERE id = $1
            """,
            asset_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Asset not found")
    return _asset_row_dict(row)


@router.post("/projects/{project_id}/assets")
async def create_asset(project_id: UUID, body: dict = Body(...)) -> dict:
    """Create an asset under a project. Expects JSON: type, name, code; optional metadata."""
    a_type = body.get("type")
    name = body.get("name")
    code = body.get("code")
    if not a_type or not name or not code:
        raise HTTPException(status_code=400, detail="type, name, and code are required")
    metadata = body.get("metadata") or {}
    pipeline_stages = _norm_pipeline_stages(body.get("pipeline_stages"))
    if "pipeline_stage_status" in body:
        pss = _norm_pipeline_stage_status(body.get("pipeline_stage_status"))
        pipeline_stages = _stages_from_status(pss)
    else:
        pss = _status_from_legacy_stages(pipeline_stages)

    async with acquire() as conn:
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO assets (project_id, type, name, code, pipeline_stages, pipeline_stage_status, metadata)
                VALUES ($1, $2, $3, $4, $5::text[], $6::jsonb, $7::jsonb)
                RETURNING id, project_id, type, name, code, pipeline_stages, pipeline_stage_status, metadata,
                          created_at, updated_at
                """,
                project_id,
                a_type,
                name,
                code,
                pipeline_stages,
                json.dumps(pss),
                json.dumps(metadata) if metadata else "{}",
            )
        except asyncpg.ForeignKeyViolationError as e:
            raise HTTPException(status_code=404, detail="Project not found") from e
        except asyncpg.UniqueViolationError as e:
            raise HTTPException(
                status_code=409,
                detail="Asset code must be unique per project",
            ) from e

    return _asset_row_dict(row)


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
    if "pipeline_stages" in body and "pipeline_stage_status" not in body:
        fields.append(f"pipeline_stages = ${len(params) + 1}::text[]")
        params.append(_norm_pipeline_stages(body["pipeline_stages"]))
    if "pipeline_stage_status" in body:
        pss = _norm_pipeline_stage_status(body.get("pipeline_stage_status"))
        fields.append(f"pipeline_stage_status = ${len(params) + 1}::jsonb")
        params.append(json.dumps(pss))
        fields.append(f"pipeline_stages = ${len(params) + 1}::text[]")
        params.append(_stages_from_status(pss))

    if not fields:
        raise HTTPException(status_code=400, detail="No updatable fields provided")

    params.append(asset_id)
    query = (
        "UPDATE assets SET "
        + ", ".join(fields)
        + " WHERE id = $"
        + str(len(params))
        + """
        RETURNING id, project_id, type, name, code, pipeline_stages, pipeline_stage_status, metadata,
                  created_at, updated_at
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

    return _asset_row_dict(row)

