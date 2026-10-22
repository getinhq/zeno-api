"""Storage integrity API (pipeline role).

- ``POST /api/v1/storage-check``  run a check for the caller's studio and keep the report
- ``GET  /api/v1/storage-check``  the last report (``null`` before the first run)
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException

from app.auth.deps import CurrentUser, require_role
from app.cas.factory import is_cas_configured
from app.db import acquire
from app.storage_check.service import run_check
from app.tenancy import current_org_id, default_org_id_for_unscoped

router = APIRouter(prefix="/api/v1/storage-check", tags=["storage-check"])


async def _org(conn):
    org = current_org_id() or await default_org_id_for_unscoped(conn)
    if org is None:
        raise HTTPException(status_code=404, detail="No studio")
    return org


@router.get("")
async def last_report(_user: CurrentUser = Depends(require_role("pipeline"))):
    async with acquire() as conn:
        raw = await conn.fetchval("SELECT last_storage_check FROM organizations WHERE id = $1", await _org(conn))
    return json.loads(raw) if isinstance(raw, str) else raw


@router.post("")
async def check_now(user: CurrentUser = Depends(require_role("pipeline"))) -> dict:
    if not is_cas_configured():
        raise HTTPException(status_code=503, detail="File storage is not configured")
    async with acquire() as conn:
        org = await _org(conn)
    report = await run_check(org)
    report["checked_by"] = user.username or None
    async with acquire() as conn:
        await conn.execute(
            "UPDATE organizations SET last_storage_check = $2::jsonb WHERE id = $1", org, json.dumps(report)
        )
    return report
