"""Redis-backed launch token storage and mint rate limiting."""
from __future__ import annotations

import json
from typing import Any, Optional

from app.redis_conn import get_redis

LAUNCH_KEY_PREFIX = "zeno:launch:token:"
RATE_KEY_PREFIX = "zeno:launch:mint_rl:"


class LaunchTokenUnavailable(Exception):
    """Redis unavailable for launch tokens."""


async def _redis():
    try:
        return await get_redis()
    except Exception as e:
        raise LaunchTokenUnavailable(str(e)) from e


async def store_token(token_id: str, payload: dict[str, Any], ttl_seconds: int) -> None:
    r = await _redis()
    key = f"{LAUNCH_KEY_PREFIX}{token_id}"
    await r.set(key, json.dumps(payload), ex=ttl_seconds)


# Atomic GET+DEL for any Redis version (GETDEL requires Redis 6.2+).
_CONSUME_LUA = """
local v = redis.call('GET', KEYS[1])
if v then redis.call('DEL', KEYS[1]) end
return v
"""


async def consume_token(token_id: str) -> Optional[dict[str, Any]]:
    """Atomically read and delete token payload. Returns None if missing or already consumed."""
    r = await _redis()
    key = f"{LAUNCH_KEY_PREFIX}{token_id}"
    raw = await r.eval(_CONSUME_LUA, 1, key)
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw)


async def check_rate_limit(client_key: str, limit_per_minute: int) -> bool:
    """
    Increment per-client mint counter. Returns True if under limit, False if exceeded.
    """
    r = await _redis()
    rk = f"{RATE_KEY_PREFIX}{client_key}"
    n = await r.incr(rk)
    if n == 1:
        await r.expire(rk, 60)
    return n <= limit_per_minute
