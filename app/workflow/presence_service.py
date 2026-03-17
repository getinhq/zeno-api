"""Redis-backed presence tracking for user sessions (and optional asset presence)."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Optional

from app.redis_conn import get_redis


class PresenceUnavailable(Exception):
    """Raised when Redis is unavailable for presence operations."""


@dataclass
class AssetRef:
    project: str
    asset: str
    representation: str


def _presence_key(user_id: str, session_id: str) -> str:
    return f"presence:{user_id}:{session_id}"


def _presence_index_key(user_id: str) -> str:
    return f"presence_index:{user_id}"


def _asset_presence_key(ref: AssetRef) -> str:
    return f"asset_presence:{ref.project}:{ref.asset}:{ref.representation}"


async def heartbeat(
    user_id: str,
    session_id: str,
    asset_ref: Optional[AssetRef],
    extra: dict[str, Any] | None,
    ttl_seconds: int = 60,
) -> None:
    """Upsert presence entry and refresh TTL; optionally record asset presence."""
    try:
        redis = await get_redis()
    except Exception as e:
        raise PresenceUnavailable(str(e)) from e

    now = datetime.now(timezone.utc).isoformat()
    value: dict[str, Any] = {
        "user_id": user_id,
        "session_id": session_id,
        "updated_at": now,
    }
    if extra:
        value.update(extra)

    key = _presence_key(user_id, session_id)
    index_key = _presence_index_key(user_id)

    try:
        await redis.set(key, value, ex=ttl_seconds)
        await redis.sadd(index_key, session_id)
        if asset_ref is not None:
            aset_key = _asset_presence_key(asset_ref)
            await redis.sadd(aset_key, session_id)
    except Exception as e:
        raise PresenceUnavailable(str(e)) from e


async def list_sessions(user_id: str) -> list[dict[str, Any]]:
    """Return active sessions for a user by reading presence keys referenced by the index set."""
    try:
        redis = await get_redis()
    except Exception as e:
        raise PresenceUnavailable(str(e)) from e

    index_key = _presence_index_key(user_id)
    try:
        session_ids = await redis.smembers(index_key)
        results: list[dict[str, Any]] = []
        stale: list[str] = []
        for sid in session_ids:
            key = _presence_key(user_id, sid)
            data = await redis.get(key)
            if data is None:
                stale.append(sid)
                continue
            # data is stored as a dict (decode_responses=True); ensure dict type
            if isinstance(data, dict):
                results.append(data)
        # Optionally clean up stale sessions from index
        if stale:
            await redis.srem(index_key, *stale)
        return results
    except Exception as e:
        raise PresenceUnavailable(str(e)) from e


async def list_asset_presence(ref: AssetRef) -> list[dict[str, Any]]:
    """Return presence entries for sessions currently associated with an asset."""
    try:
        redis = await get_redis()
    except Exception as e:
        raise PresenceUnavailable(str(e)) from e

    aset_key = _asset_presence_key(ref)
    try:
        session_ids = await redis.smembers(aset_key)
        results: list[dict[str, Any]] = []
        for sid in session_ids:
            # For asset presence we don't know user_id directly; store user_id in the presence payload
            # and scan presence keys for this session_id. For 0.6, approximate by reading all users via pattern.
            # To avoid KEYS, assume clients call list_sessions(user_id) for detailed info.
            # Here we just return minimal info: session_id.
            results.append({"session_id": sid})
        return results
    except Exception as e:
        raise PresenceUnavailable(str(e)) from e

