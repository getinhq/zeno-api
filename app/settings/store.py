"""MongoDB-backed settings store for global and per-project config.

Collections:
- settings_global: one document per environment ("production", "staging", "development")
- settings_project: one document per project (UUID from Postgres)

This module is intentionally thin: it exposes helpers that other parts of the
pipeline can call, and hides MongoDB details behind simple functions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

from pymongo import MongoClient
from pymongo.errors import ConfigurationError

from app.config import MONGO_DB_NAME, MONGO_URI


DEFAULT_TTL_SECONDS = 60


@dataclass
class SettingsCacheEntry:
    value: Dict[str, Any]
    expires_at: datetime


_client: Optional[MongoClient] = None
_cache_global: Dict[str, SettingsCacheEntry] = {}
_cache_project: Dict[str, SettingsCacheEntry] = {}


def _get_client() -> MongoClient:
    """Return a process-wide MongoClient. Uses MONGO_URI loaded at startup; restart the API after changing .env."""
    global _client
    if _client is None:
        if not MONGO_URI:
            raise RuntimeError("MONGO_URI is not configured; settings store unavailable")
        _client = MongoClient(MONGO_URI)
    return _client


def _get_db():
    """Return the MongoDB database used for settings.

    Uses the database from MONGO_URI path if present (e.g. ...mongodb.net/zeno_db).
    If the URI has no database, uses ZENO_MONGO_DB (default: zeno_db).
    """
    client = _get_client()
    try:
        return client.get_default_database()
    except ConfigurationError:
        return client[MONGO_DB_NAME]


def get_mongo_db():
    """Shared Mongo database handle (settings, manifests, etc.)."""
    return _get_db()


def _now() -> datetime:
    return datetime.utcnow()


def _deep_merge(base: Dict[str, Any], overrides: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge overrides into base; returns a new dict."""
    result: Dict[str, Any] = dict(base)
    for key, value in overrides.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def get_global_settings(env: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> Dict[str, Any]:
    """Return global settings for an environment.

    If no document exists, returns a basic default.
    Results are cached in-process for ttl_seconds.
    """
    now = _now()
    entry = _cache_global.get(env)
    if entry and entry.expires_at > now:
        return entry.value

    db = _get_db()
    doc = db["settings_global"].find_one({"env": env}) or {}

    if not doc:
        # Very minimal defaults; callers can layer their own defaults on top.
        doc = {
            "env": env,
            "resolution": {"width": 1920, "height": 1080},
            "frame": {"rate": 24.0, "handle_in": 0, "handle_out": 0},
            "qc_checks": [],
            "extra": {},
        }

    entry = SettingsCacheEntry(value=doc, expires_at=now + timedelta(seconds=ttl_seconds))
    _cache_global[env] = entry
    return entry.value


def get_project_settings(project_id: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> Optional[Dict[str, Any]]:
    """Return project-specific settings document or None if not present."""
    now = _now()
    entry = _cache_project.get(project_id)
    if entry and entry.expires_at > now:
        return entry.value

    db = _get_db()
    doc = db["settings_project"].find_one({"project_id": project_id})
    if not doc:
        _cache_project.pop(project_id, None)
        return None

    entry = SettingsCacheEntry(value=doc, expires_at=now + timedelta(seconds=ttl_seconds))
    _cache_project[project_id] = entry
    return entry.value


def get_effective_settings(env: str, project_id: Optional[str]) -> Dict[str, Any]:
    """Return the effective settings for a given env and project.

    - Start from global settings for env.
    - If a project settings document exists with an 'overrides' object,
      deep-merge that into global (project overrides win).
    """
    global_settings = get_global_settings(env)
    effective = dict(global_settings)

    if not project_id:
        return effective

    project_doc = get_project_settings(project_id)
    if not project_doc:
        return effective

    overrides = project_doc.get("overrides") or {}
    if isinstance(overrides, dict):
        effective = _deep_merge(effective, overrides)
    return effective


def upsert_global_settings(env: str, doc: Dict[str, Any]) -> Dict[str, Any]:
    """Insert or replace global settings for an environment. Invalidates cache."""
    if env not in ("production", "staging", "development"):
        raise ValueError("env must be production, staging, or development")
    _cache_global.pop(env, None)
    db = _get_db()
    now = _now()
    payload = {
        "env": env,
        "resolution": doc.get("resolution", {"width": 1920, "height": 1080}),
        "frame": doc.get("frame", {"rate": 24.0, "handle_in": 0, "handle_out": 0}),
        "qc_checks": doc.get("qc_checks", []),
        "extra": doc.get("extra", {}),
        "updated_at": now,
    }
    db["settings_global"].update_one(
        {"env": env},
        {"$set": payload},
        upsert=True,
    )
    out = dict(payload)
    out["updated_at"] = now.isoformat() + "Z"
    return out


def upsert_project_settings(project_id: str, overrides: Optional[Dict[str, Any]] = None, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Insert or replace project settings. Invalidates cache."""
    _cache_project.pop(project_id, None)
    db = _get_db()
    now = _now()
    payload = {
        "project_id": project_id,
        "overrides": overrides if overrides is not None else {},
        "extra": extra if extra is not None else {},
        "updated_at": now,
    }
    db["settings_project"].update_one(
        {"project_id": project_id},
        {"$set": payload},
        upsert=True,
    )
    return {"project_id": project_id, "overrides": payload["overrides"], "extra": payload["extra"], "updated_at": now.isoformat() + "Z"}


def ensure_settings_indexes() -> None:
    """Create indexes on settings_global (env unique) and settings_project (project_id unique).
    Safe to call at startup; creates collections if they don't exist.
    """
    db = _get_db()
    db["settings_global"].create_index("env", unique=True)
    db["settings_project"].create_index("project_id", unique=True)

