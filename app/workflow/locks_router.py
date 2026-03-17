"""HTTP API for Redis-backed locks on asset representations."""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.workflow.lock_service import (
    LockHeldByOther,
    LockNotFound,
    LockNotOwned,
    LockUnavailable,
    acquire_lock,
    get_lock_status,
    release_lock,
)

router = APIRouter(prefix="/api/v1", tags=["locks"])


class LockRequest(BaseModel):
    user_id: str = Field(..., description="User identifier")
    session_id: str = Field(..., description="Session identifier")
    project: str = Field(..., description="Project identifier (code or UUID)")
    asset: str = Field(..., description="Asset identifier (code or UUID)")
    representation: str = Field(..., description="Representation key (e.g. model, fbx)")


class LockStatusResponse(BaseModel):
    project: str
    asset: str
    representation: str
    owner_user_id: str
    owner_session_id: str
    acquired_at: str


LOCK_TTL_SECONDS_DEFAULT = 600


@router.post("/locks/acquire", response_model=LockStatusResponse)
async def locks_acquire(body: LockRequest) -> Any:
    """Acquire a lock for a project/asset/representation; hard-fail if held by another session."""
    try:
        info = await acquire_lock(
            user_id=body.user_id,
            session_id=body.session_id,
            project=body.project,
            asset=body.asset,
            representation=body.representation,
            ttl_seconds=LOCK_TTL_SECONDS_DEFAULT,
        )
    except LockHeldByOther as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except LockUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    return info


@router.post("/locks/release")
async def locks_release(body: LockRequest) -> dict[str, str]:
    """Release a lock if owned by the caller."""
    try:
        await release_lock(
            user_id=body.user_id,
            session_id=body.session_id,
            project=body.project,
            asset=body.asset,
            representation=body.representation,
        )
    except LockNotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except LockNotOwned as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except LockUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    return {"status": "ok"}


@router.get("/locks/status", response_model=Optional[LockStatusResponse])
async def locks_status(
    project: str = Query(..., description="Project identifier"),
    asset: str = Query(..., description="Asset identifier"),
    representation: str = Query(..., description="Representation key"),
):
    """Get current lock status for a resource."""
    try:
        info = await get_lock_status(project=project, asset=asset, representation=representation)
    except LockUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    if info is None:
        raise HTTPException(status_code=404, detail="No lock for this resource")
    return info

