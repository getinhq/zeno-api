"""Redis connection helper for workflow (presence/locks)."""
from __future__ import annotations

from typing import Optional

from redis.asyncio import Redis

from app.config import REDIS_URL

_redis: Optional[Redis] = None


async def get_redis() -> Redis:
    """Return a singleton Redis client based on REDIS_URL."""
    global _redis
    if _redis is None:
        if not REDIS_URL:
            raise RuntimeError("REDIS_URL is not set")
        _redis = Redis.from_url(REDIS_URL, decode_responses=True)
    return _redis


async def close_redis() -> None:
    """Close the Redis client, if any."""
    global _redis
    if _redis is not None:
        await _redis.close()
        _redis = None

