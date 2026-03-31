"""Mint and exchange launch tokens; optional open lock pre-check."""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel

import app.config as app_config
from app.launch.dcc_settings import resolve_dcc_executable_path
from app.launch.models import (
    ExchangeLaunchTokenResponse,
    LaunchContextV1,
    MintLaunchTokenBody,
    MintLaunchTokenResponse,
)
from app.launch.service import LaunchTokenUnavailable, check_rate_limit, consume_token, store_token
from app.workflow.lock_service import LockUnavailable, get_lock_status

router = APIRouter(prefix="/api/v1", tags=["launch"])


def _client_key(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


def _require_mint_secret(x_zeno_launch_mint_key: Optional[str]) -> None:
    secret = app_config.ZENO_LAUNCH_MINT_SECRET
    env = app_config.APP_ENV
    if secret:
        if x_zeno_launch_mint_key != secret:
            raise HTTPException(status_code=401, detail="Invalid launch mint credentials")
        return
    if env in ("production", "staging"):
        raise HTTPException(
            status_code=503,
            detail="ZENO_LAUNCH_MINT_SECRET must be set to mint launch tokens in this environment",
        )


@router.post("/launch-tokens", response_model=MintLaunchTokenResponse)
async def mint_launch_token(
    request: Request,
    body: MintLaunchTokenBody,
    x_zeno_launch_mint_key: Optional[str] = Header(None, alias="X-Zeno-Launch-Mint-Key"),
) -> MintLaunchTokenResponse:
    """
    Mint a short-lived opaque token. Caller must be authorized to open the target resource
    (enforce via gateway/auth when integrated). Protected by X-Zeno-Launch-Mint-Key in production.
    """
    _require_mint_secret(x_zeno_launch_mint_key)

    ok = await check_rate_limit(
        _client_key(request),
        app_config.LAUNCH_TOKEN_RATE_LIMIT_PER_MINUTE,
    )
    if not ok:
        raise HTTPException(status_code=429, detail="Launch token mint rate limit exceeded")

    token_id = secrets.token_urlsafe(32)
    ttl = app_config.LAUNCH_TOKEN_TTL_SECONDS
    ctx_dict = body.context.model_dump(mode="json", exclude_none=True)
    if not ctx_dict.get("dcc_executable_path"):
        resolved = resolve_dcc_executable_path(
            str(ctx_dict.get("dcc", "")),
            ctx_dict.get("dcc_label"),
        )
        if resolved:
            ctx_dict["dcc_executable_path"] = resolved
    payload = {"context": ctx_dict}

    try:
        await store_token(token_id, payload, ttl)
    except LaunchTokenUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e)) from e

    exp = datetime.now(timezone.utc) + timedelta(seconds=ttl)
    return MintLaunchTokenResponse(token=token_id, expires_at=exp.isoformat())


@router.get("/launch-tokens/{token}", response_model=ExchangeLaunchTokenResponse)
async def exchange_launch_token(token: str, response: Response) -> ExchangeLaunchTokenResponse:
    """
    One-time exchange: returns launch context and consumes the token.
    """
    try:
        data = await consume_token(token)
    except LaunchTokenUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e)) from e

    if data is None:
        raise HTTPException(status_code=410, detail="Launch token expired or already consumed")

    ctx_raw = data.get("context")
    if not isinstance(ctx_raw, dict):
        raise HTTPException(status_code=500, detail="Invalid launch token payload")

    try:
        ctx = LaunchContextV1.model_validate(ctx_raw)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Invalid launch context: {e}") from e

    response.headers["Cache-Control"] = "no-store"
    return ExchangeLaunchTokenResponse(context=ctx)


class OpenLockCheckResponse(BaseModel):
    blocked: bool
    lock: Optional[dict[str, Any]] = None


@router.get("/launch/open-lock-check", response_model=OpenLockCheckResponse)
async def open_lock_check(
    project: str = Query(...),
    asset: str = Query(...),
    representation: str = Query(...),
) -> OpenLockCheckResponse:
    """
    Returns whether an open would be blocked by an existing lock (read-only; does not acquire).
    """
    try:
        info = await get_lock_status(project=project, asset=asset, representation=representation)
    except LockUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    if info is None:
        return OpenLockCheckResponse(blocked=False, lock=None)
    return OpenLockCheckResponse(blocked=True, lock=info)
