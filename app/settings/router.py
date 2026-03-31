"""Settings API — global and per-project settings (MongoDB)."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Body, HTTPException

from app.settings.store import (
    get_effective_settings,
    get_global_settings,
    get_project_settings,
    upsert_global_settings,
    upsert_project_settings,
)

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])


def _json_safe(d: dict) -> dict:
    """Make MongoDB doc JSON-serializable (e.g. _id -> str)."""
    out = dict(d)
    if "_id" in out and hasattr(out["_id"], "__str__"):
        out["_id"] = str(out["_id"])
    return out


@router.get("/global")
async def get_global(env: str = "development") -> dict:
    """Get global settings for an environment (production, staging, development)."""
    if env not in ("production", "staging", "development"):
        raise HTTPException(status_code=400, detail="env must be production, staging, or development")
    try:
        return _json_safe(get_global_settings(env))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@router.get("/project/{project_id}")
async def get_project_settings_overrides(project_id: str) -> dict:
    """Get project-specific settings document, or 404 if none."""
    doc = get_project_settings(project_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="No project settings found")
    return _json_safe(doc)


@router.get("/effective")
async def get_effective(env: str = "development", project_id: Optional[str] = None) -> dict:
    """Get effective settings (global merged with project overrides)."""
    if env not in ("production", "staging", "development"):
        raise HTTPException(status_code=400, detail="env must be production, staging, or development")
    try:
        return _json_safe(get_effective_settings(env, project_id))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@router.put("/global")
async def put_global(env: str = "development", body: dict = Body(...)) -> dict:
    """Upsert global settings for an environment. Body: resolution?, frame?, qc_checks?, extra?."""
    if env not in ("production", "staging", "development"):
        raise HTTPException(status_code=400, detail="env must be production, staging, or development")
    try:
        return upsert_global_settings(env, body)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Settings store error: {str(e)}") from e


@router.put("/project/{project_id}")
async def put_project_settings_overrides(project_id: str, body: dict = Body(...)) -> dict:
    """Upsert project settings. Body: overrides?, extra?."""
    try:
        overrides = body.get("overrides")
        extra = body.get("extra")
        return upsert_project_settings(project_id, overrides=overrides, extra=extra)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Settings store error: {str(e)}") from e
