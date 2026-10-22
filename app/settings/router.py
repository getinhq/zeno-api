"""Settings API — global and per-project settings (MongoDB)."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException

from app.auth.deps import require_role
from app.db import acquire
from app.tenancy import ensure_in_scope, settings_key
from app.settings.store import (
    get_effective_settings,
    get_global_settings,
    get_project_settings,
    upsert_global_settings,
    upsert_project_settings,
)

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])
ALLOWED_STAGES = {"Animatics", "Layout", "Animation", "Lighting", "Comp"}


def _json_safe(d: dict) -> dict:
    """Make MongoDB doc JSON-serializable (e.g. _id -> str)."""
    out = dict(d)
    if "_id" in out and hasattr(out["_id"], "__str__"):
        out["_id"] = str(out["_id"])
    return out


def _validate_stage_mapping(extra: Optional[dict]) -> None:
    if not isinstance(extra, dict):
        return
    mapping = extra.get("stage_dcc_mapping")
    if mapping is None:
        return
    if not isinstance(mapping, dict):
        raise HTTPException(status_code=400, detail="extra.stage_dcc_mapping must be an object")
    invalid_keys = [k for k in mapping.keys() if k not in ALLOWED_STAGES]
    if invalid_keys:
        raise HTTPException(
            status_code=400,
            detail=f"stage_dcc_mapping keys must be one of: {', '.join(sorted(ALLOWED_STAGES))}",
        )
    invalid_values = [v for v in mapping.values() if not isinstance(v, str) or not v.strip()]
    if invalid_values:
        raise HTTPException(status_code=400, detail="stage_dcc_mapping values must be non-empty strings")


async def _ensure_project(project_id: str) -> None:
    async with acquire() as conn:
        await ensure_in_scope(conn, "project", project_id)


@router.get("/global")
async def get_global(env: str = "development") -> dict:
    """Get global settings for an environment (production, staging, development)."""
    if env not in ("production", "staging", "development"):
        raise HTTPException(status_code=400, detail="env must be production, staging, or development")
    try:
        return _json_safe(get_global_settings(settings_key(env)))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@router.get("/project/{project_id}")
async def get_project_settings_overrides(project_id: str) -> dict:
    """Get project-specific settings document, or 404 if none."""
    await _ensure_project(project_id)
    doc = get_project_settings(project_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="No project settings found")
    return _json_safe(doc)


@router.get("/effective")
async def get_effective(env: str = "development", project_id: Optional[str] = None) -> dict:
    """Get effective settings (global merged with project overrides)."""
    if env not in ("production", "staging", "development"):
        raise HTTPException(status_code=400, detail="env must be production, staging, or development")
    if project_id:
        await _ensure_project(project_id)
    try:
        return _json_safe(get_effective_settings(settings_key(env), project_id))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@router.put("/global")
async def put_global(
    env: str = "development",
    body: dict = Body(...),
    _user=Depends(require_role("pipeline")),
) -> dict:
    """Upsert global settings for an environment. Body: resolution?, frame?, qc_checks?, extra?."""
    if env not in ("production", "staging", "development"):
        raise HTTPException(status_code=400, detail="env must be production, staging, or development")
    try:
        return upsert_global_settings(settings_key(env), body)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Settings store error: {str(e)}") from e


@router.put("/project/{project_id}")
async def put_project_settings_overrides(
    project_id: str,
    body: dict = Body(...),
    _user=Depends(require_role("pipeline")),
) -> dict:
    """Upsert project settings. Body: overrides?, extra?."""
    await _ensure_project(project_id)
    try:
        overrides = body.get("overrides")
        extra = body.get("extra")
        _validate_stage_mapping(extra)
        return upsert_project_settings(project_id, overrides=overrides, extra=extra)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Settings store error: {str(e)}") from e
