"""Redis-backed locks for asset representations."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from app.redis_conn import get_redis


def _decode_lock_value(raw: Any) -> Optional[dict[str, Any]]:
    """Parse Redis GET payload (JSON string) or legacy dict."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if isinstance(raw, str):
        try:
            out = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return out if isinstance(out, dict) else None
    return None


class LockUnavailable(Exception):
    """Raised when Redis is unavailable for lock operations."""


class LockHeldByOther(Exception):
    """Raised when another session already holds the lock."""


class LockNotOwned(Exception):
    """Raised when a non-owner attempts to release a lock."""


class LockNotFound(Exception):
    """Raised when a lock does not exist."""


def _lock_key(project: str, asset: str, representation: str) -> str:
    return f"lock:{project}:{asset}:{representation}"


async def acquire_lock(
    user_id: str,
    session_id: str,
    project: str,
    asset: str,
    representation: str,
    ttl_seconds: int,
) -> dict[str, Any]:
    """Acquire a lock for a resource; hard-fail if held by another session."""
    try:
        redis = await get_redis()
    except Exception as e:
        raise LockUnavailable(str(e)) from e

    key = _lock_key(project, asset, representation)
    now = datetime.now(timezone.utc).isoformat()
    value = {
        "owner_user_id": user_id,
        "owner_session_id": session_id,
        "acquired_at": now,
    }

    try:
        # Try to create the lock if it does not exist (redis-py 5+ requires string/blob, not dict)
        payload = json.dumps(value, separators=(",", ":"))
        created = await redis.set(key, payload, ex=ttl_seconds, nx=True)
        if created:
            return {**value, "project": project, "asset": asset, "representation": representation}

        # Lock exists; check ownership
        existing = _decode_lock_value(await redis.get(key))
        if existing is None:
            raise LockUnavailable("Lock value has unexpected format")

        if (
            existing.get("owner_user_id") == user_id
            and existing.get("owner_session_id") == session_id
        ):
            # Idempotent acquire: refresh TTL and return existing
            await redis.expire(key, ttl_seconds)
            return {**existing, "project": project, "asset": asset, "representation": representation}

        raise LockHeldByOther(
            f"Lock already held by user {existing.get('owner_user_id')} session {existing.get('owner_session_id')}"
        )
    except LockHeldByOther:
        raise
    except Exception as e:
        raise LockUnavailable(str(e)) from e


async def release_lock(
    user_id: str,
    session_id: str,
    project: str,
    asset: str,
    representation: str,
) -> None:
    """Release a lock if owned by the caller."""
    try:
        redis = await get_redis()
    except Exception as e:
        raise LockUnavailable(str(e)) from e

    key = _lock_key(project, asset, representation)
    try:
        existing = _decode_lock_value(await redis.get(key))
        if existing is None:
            raise LockNotFound("Lock does not exist")
        if (
            existing.get("owner_user_id") != user_id
            or existing.get("owner_session_id") != session_id
        ):
            raise LockNotOwned("Lock is held by another session")
        await redis.delete(key)
    except (LockNotFound, LockNotOwned):
        raise
    except Exception as e:
        raise LockUnavailable(str(e)) from e


async def get_lock_status(
    project: str,
    asset: str,
    representation: str,
) -> Optional[dict[str, Any]]:
    """Return current lock info for a resource, or None if not locked."""
    try:
        redis = await get_redis()
    except Exception as e:
        raise LockUnavailable(str(e)) from e

    key = _lock_key(project, asset, representation)
    try:
        existing = _decode_lock_value(await redis.get(key))
        if existing is None:
            return None
        # Include resource identifiers
        existing = {
            "project": project,
            "asset": asset,
            "representation": representation,
            **existing,
        }
        return existing
    except Exception as e:
        raise LockUnavailable(str(e)) from e

