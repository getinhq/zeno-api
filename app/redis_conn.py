"""Redis connection helper for workflow (presence/locks)."""
from __future__ import annotations

from typing import Optional

from redis.asyncio import Redis

import app.config as app_config

_redis: Optional[Redis] = None


async def get_redis() -> Redis:
    """Return a singleton Redis client based on REDIS_URL."""
    global _redis
    if _redis is None:
        if not app_config.REDIS_URL:
            raise RuntimeError("REDIS_URL is not set")
        _redis = Redis.from_url(app_config.REDIS_URL, decode_responses=True)
    return _redis


async def close_redis() -> None:
    """Close the Redis client, if any."""
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None

