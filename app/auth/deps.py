"""FastAPI dependencies for authenticated requests.

- ``get_current_user`` parses the ``Authorization: Bearer <jwt>`` header,
  validates the token and returns a small dataclass. Rejects expired,
  wrong-type (refresh) or malformed tokens with 401.
- ``require_role`` composes over it to enforce a minimum set of app roles.
- ``optional_current_user`` is used by routes that stay readable to unauth'd
  callers (e.g. /health) but still want to know who's calling.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional
from uuid import UUID

import jwt
from fastapi import Depends, Header, HTTPException, status

from app.auth import service as auth_service


@dataclass
class CurrentUser:
    id: UUID
    username: str
    app_role: Optional[str]
    jti: str


def _parse_bearer(authorization: Optional[str]) -> str:
    if not authorization:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Malformed Authorization header")
    return parts[1].strip()


def _decode_access(token: str) -> dict:
    try:
        payload = auth_service.decode_token(token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")
    except jwt.PyJWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    if payload.get("typ") != "access":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Wrong token type")
    return payload


async def get_current_user(authorization: Optional[str] = Header(default=None)) -> CurrentUser:
    token = _parse_bearer(authorization)
    payload = _decode_access(token)
    try:
        uid = UUID(str(payload.get("sub")))
    except (TypeError, ValueError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token subject")
    return CurrentUser(
        id=uid,
        username=str(payload.get("username") or ""),
        app_role=payload.get("app_role"),
        jti=str(payload.get("jti") or ""),
    )


async def optional_current_user(
    authorization: Optional[str] = Header(default=None),
) -> Optional[CurrentUser]:
    if not authorization:
        return None
    try:
        return await get_current_user(authorization=authorization)
    except HTTPException:
        return None


def require_role(*roles: str):
    """Return a FastAPI dependency that 403s unless the current user matches.

    When ``ENABLE_AUTH`` is false at startup, the guard degrades to an
    "identify yourself best-effort" dependency — legacy dev flows keep
    working and tests written before auth existed still pass.
    """
    import app.config as app_config

    allowed = {r.lower() for r in roles}

    async def _dep(authorization: Optional[str] = Header(default=None)) -> CurrentUser:
        if not app_config.ENABLE_AUTH:
            user = await optional_current_user(authorization=authorization)
            if user is None:
                return CurrentUser(
                    id=UUID("00000000-0000-0000-0000-000000000000"),
                    username="__anonymous__",
                    app_role=next(iter(allowed)),
                    jti="",
                )
            return user
        user = await get_current_user(authorization=authorization)
        role = (user.app_role or "").lower()
        if role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires one of roles: {sorted(allowed)}",
            )
        return user

    return _dep


MANAGEMENT_ROLES: tuple[str, ...] = ("pipeline", "supervisor", "production")


def require_management():
    return require_role(*MANAGEMENT_ROLES)


async def require_current_user(
    authorization: Optional[str] = Header(default=None),
) -> CurrentUser:
    """Authenticated-only dependency that respects ``ENABLE_AUTH``.

    When the feature flag is off, returns an anonymous stub so existing
    endpoints (and legacy tests) keep working.
    """
    import app.config as app_config

    if not app_config.ENABLE_AUTH:
        user = await optional_current_user(authorization=authorization)
        if user is None:
            return CurrentUser(
                id=UUID("00000000-0000-0000-0000-000000000000"),
                username="__anonymous__",
                app_role="pipeline",
                jti="",
            )
        return user
    return await get_current_user(authorization=authorization)
