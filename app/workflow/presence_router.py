"""HTTP API for Redis-backed presence tracking."""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.workflow.presence_service import (
    AssetRef,
    PresenceUnavailable,
    heartbeat,
    list_asset_presence,
    list_sessions,
)

router = APIRouter(prefix="/api/v1", tags=["presence"])


class PresenceHeartbeatRequest(BaseModel):
    user_id: str = Field(..., description="User identifier (code, email, or UUID)")
    session_id: str = Field(..., description="Opaque session identifier")
    project: Optional[str] = Field(
        None, description="Optional project identifier (code or UUID) for asset presence"
    )
    asset: Optional[str] = Field(
        None, description="Optional asset identifier (code or UUID) for asset presence"
    )
    representation: Optional[str] = Field(
        None, description="Optional representation key (e.g. model, fbx) for asset presence"
    )
    metadata: Optional[dict[str, Any]] = Field(
        None, description="Optional extra metadata (e.g. ip, host, dcc app)"
    )


class PresenceSession(BaseModel):
    user_id: str
    session_id: str
    updated_at: str
    metadata: Optional[dict[str, Any]] = None


@router.post("/presence/heartbeat")
async def presence_heartbeat(body: PresenceHeartbeatRequest) -> dict[str, str]:
    """Upsert a presence key for a user/session and refresh TTL."""
    asset_ref: Optional[AssetRef] = None
    if body.project and body.asset and body.representation:
        asset_ref = AssetRef(
            project=body.project,
            asset=body.asset,
            representation=body.representation,
        )
    extra = body.metadata or {}
    try:
        await heartbeat(body.user_id, body.session_id, asset_ref, extra)
    except PresenceUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    return {"status": "ok"}


@router.get("/presence/sessions", response_model=list[PresenceSession])
async def presence_sessions(user_id: str = Query(..., description="User identifier")):
    """List active sessions for a user."""
    try:
        sessions = await list_sessions(user_id)
    except PresenceUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    # Normalize metadata field
    normalized: list[dict[str, Any]] = []
    for s in sessions:
        user = s.get("user_id", user_id)
        sid = s.get("session_id")
        updated_at = s.get("updated_at", "")
        meta = {k: v for k, v in s.items() if k not in {"user_id", "session_id", "updated_at"}}
        normalized.append(
            {
                "user_id": user,
                "session_id": sid,
                "updated_at": updated_at,
                "metadata": meta or None,
            }
        )
    return normalized


@router.get("/presence/asset")
async def presence_asset(
    project: str = Query(..., description="Project identifier"),
    asset: str = Query(..., description="Asset identifier"),
    representation: str = Query(..., description="Representation key"),
) -> dict[str, Any]:
    """List session_ids currently associated with a given asset representation."""
    ref = AssetRef(project=project, asset=asset, representation=representation)
    try:
        sessions = await list_asset_presence(ref)
    except PresenceUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    return {"sessions": sessions}

