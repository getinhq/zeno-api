"""Password hashing + JWT helpers.

- Hashing uses passlib-bcrypt so existing hashes stay portable even if we swap
  the algorithm later.
- JWTs are HS256 signed with ``ZENO_JWT_SECRET``. Tests and dev boot generate
  a throwaway secret so the suite stays self-contained; production rejects a
  missing secret at startup (see ``main.py``).
- Access tokens carry ``sub`` (user id), ``username`` and ``app_role`` so
  route guards don't have to round-trip to the DB on every call.
- Refresh tokens are a distinct ``typ='refresh'`` token with their own TTL;
  ``jti`` lets us blacklist them via Redis on logout.
"""
from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID, uuid4

import bcrypt
import jwt

import app.config as app_config

# bcrypt truncates at 72 bytes; we normalize by hashing longer passwords with
# SHA-256 first so we never trip the passlib/bcrypt wrap-bug and still preserve
# entropy.
_BCRYPT_MAX = 72

_TEST_SECRET: Optional[str] = None


def _secret() -> str:
    """Return the active JWT secret.

    In production ``ZENO_JWT_SECRET`` is required and validated at startup.
    In dev/test we generate and cache a random secret on first use so tokens
    round-trip within a single process without operator setup.
    """
    global _TEST_SECRET
    if app_config.ZENO_JWT_SECRET:
        return app_config.ZENO_JWT_SECRET
    if _TEST_SECRET is None:
        _TEST_SECRET = secrets.token_urlsafe(48)
        os.environ.setdefault("ZENO_JWT_SECRET_EPHEMERAL", _TEST_SECRET)
    return _TEST_SECRET


def _prepare_secret(plain: str) -> bytes:
    raw = plain.encode("utf-8")
    if len(raw) > _BCRYPT_MAX:
        import hashlib

        raw = hashlib.sha256(raw).hexdigest().encode("ascii")
    return raw


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(_prepare_secret(plain), bcrypt.gensalt()).decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(_prepare_secret(plain), hashed.encode("ascii"))
    except Exception:  # noqa: BLE001
        return False


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def issue_access_token(
    user_id: UUID | str,
    username: str,
    app_role: Optional[str],
    *,
    ttl_seconds: Optional[int] = None,
) -> tuple[str, datetime]:
    exp = _now() + timedelta(seconds=ttl_seconds or app_config.ZENO_ACCESS_TOKEN_TTL_SECONDS)
    payload = {
        "sub": str(user_id),
        "username": username,
        "app_role": app_role,
        "typ": "access",
        "iat": int(_now().timestamp()),
        "exp": int(exp.timestamp()),
        "jti": uuid4().hex,
    }
    return jwt.encode(payload, _secret(), algorithm="HS256"), exp


def issue_refresh_token(
    user_id: UUID | str,
    *,
    ttl_seconds: Optional[int] = None,
) -> tuple[str, datetime, str]:
    exp = _now() + timedelta(seconds=ttl_seconds or app_config.ZENO_REFRESH_TOKEN_TTL_SECONDS)
    jti = uuid4().hex
    payload = {
        "sub": str(user_id),
        "typ": "refresh",
        "iat": int(_now().timestamp()),
        "exp": int(exp.timestamp()),
        "jti": jti,
    }
    return jwt.encode(payload, _secret(), algorithm="HS256"), exp, jti


def decode_token(token: str) -> dict[str, Any]:
    """Decode + validate signature/exp. Raises ``jwt.PyJWTError`` on failure."""
    return jwt.decode(token, _secret(), algorithms=["HS256"])
