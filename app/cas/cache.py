"""Redis cache for CAS existence checks.

Only "exists" is cached: a stored blob never changes, while a "missing" answer goes stale the
moment someone uploads it (caching it made clients re-upload data the server already had).
Whatever deletes blobs (garbage collection) must call ``forget_exists``.
"""
from __future__ import annotations

from app.config import MANIFEST_CACHE_TTL_SECONDS
from app.redis_conn import get_redis

KEY_PREFIX = "cas_exists:v1:"


def _key(content_hash: str) -> str:
    return f"{KEY_PREFIX}{content_hash.strip().lower()}"


async def get_cached_exists(content_hash: str) -> bool | None:
    redis = await get_redis()
    raw = await redis.get(_key(content_hash))
    if raw is None:
        return None
    if raw == "1":
        return True
    return None


async def set_cached_exists(content_hash: str, exists: bool) -> None:
    redis = await get_redis()
    if not exists:
        await redis.delete(_key(content_hash))
        return
    await redis.set(_key(content_hash), "1", ex=MANIFEST_CACHE_TTL_SECONDS)


async def forget_exists(content_hash: str) -> None:
    redis = await get_redis()
    await redis.delete(_key(content_hash))
