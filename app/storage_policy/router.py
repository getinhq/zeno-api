"""Storage workflow API.

- ``GET  /api/v1/storage-policy``                  the studio policy, resolved per extension, plus the catalog
- ``GET  /api/v1/storage-policy/resolve?filename=`` what happens to one file (DCC plugins call this)
- ``PUT  /api/v1/storage-policy``                  change switches/rules (pipeline role)
- ``POST /api/v1/storage-policy/reset``            back to the recommended defaults (pipeline role)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from app.auth.deps import CurrentUser, require_current_user, require_role
from app.db import acquire
from app.storage_policy import policy
from app.tenancy import current_org_id, default_org_id_for_unscoped

router = APIRouter(prefix="/api/v1/storage-policy", tags=["storage-policy"])


async def _org_id(conn) -> UUID:
    org = current_org_id() or await default_org_id_for_unscoped(conn)
    if org is None:
        raise HTTPException(status_code=404, detail="No studio")
    return org


async def load_for_org(conn, org_id: Optional[UUID]) -> dict:
    if org_id is None:
        return {}
    raw = await conn.fetchval("SELECT storage_policy FROM organizations WHERE id = $1", org_id)
    if isinstance(raw, str):
        raw = json.loads(raw or "{}")
    return raw or {}


async def load_for_project(conn, project_id) -> dict:
    org_id = await conn.fetchval("SELECT organization_id FROM projects WHERE id = $1", project_id)
    return await load_for_org(conn, org_id)


@router.get("")
async def get_policy(_user: CurrentUser = Depends(require_current_user)) -> dict:
    async with acquire() as conn:
        return policy.describe(await load_for_org(conn, await _org_id(conn)))


@router.get("/resolve")
async def resolve_file(
    filename: str = Query(..., description="File name or extension, e.g. shot010.blend or .exr"),
    _user: CurrentUser = Depends(require_current_user),
) -> dict:
    async with acquire() as conn:
        stored = await load_for_org(conn, await _org_id(conn))
    out = policy.resolve(stored, filename)
    out["algorithm_version"] = policy.ALGORITHM_VERSION
    return out


async def _save(stored: dict, user: CurrentUser) -> dict:
    stored = {
        **stored,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "updated_by": user.username or None,
    }
    async with acquire() as conn:
        org = await _org_id(conn)
        await conn.execute("UPDATE organizations SET storage_policy = $2::jsonb WHERE id = $1", org, json.dumps(stored))
    return policy.describe(stored)


@router.put("")
async def put_policy(body: dict = Body(...), user: CurrentUser = Depends(require_role("pipeline"))) -> dict:
    """Partial update: send any of chunking_enabled, default_scene_strategy, rules (replaces all rules)."""
    clean = policy.validate(body)
    async with acquire() as conn:
        stored = await load_for_org(conn, await _org_id(conn))
    return await _save({**stored, **clean}, user)


@router.post("/reset")
async def reset_policy(user: CurrentUser = Depends(require_role("pipeline"))) -> dict:
    return await _save({}, user)
