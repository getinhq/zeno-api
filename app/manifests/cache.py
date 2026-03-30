"""Redis read-through cache for manifest documents."""
from __future__ import annotations

import json
from typing import Any, Optional

from app.config import MANIFEST_CACHE_TTL_SECONDS
from app.redis_conn import get_redis

KEY_PREFIX = "manifest_cache:v1:"


def _key(manifest_hash: str) -> str:
    return f"{KEY_PREFIX}{manifest_hash.strip().lower()}"


async def get_cached_manifest(manifest_hash: str) -> Optional[dict[str, Any]]:
    """Return cached manifest document, or None."""
    redis = await get_redis()
    raw = await redis.get(_key(manifest_hash))
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


async def set_cached_manifest(manifest_hash: str, doc: dict[str, Any]) -> None:
    """Cache manifest JSON with TTL."""
    redis = await get_redis()
    await redis.set(_key(manifest_hash), json.dumps(doc, separators=(",", ":")), ex=MANIFEST_CACHE_TTL_SECONDS)

