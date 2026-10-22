"""Personal access tokens: ``zeno_pat_<random>``, stored as SHA-256, revocable, optional expiry."""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

PREFIX = "zeno_pat_"


def is_pat(token: str) -> bool:
    return token.startswith(PREFIX)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def create(conn, user_id: UUID, name: str, ttl: Optional[timedelta] = None) -> tuple[str, Any]:
    token = PREFIX + secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + ttl if ttl else None
    row = await conn.fetchrow(
        """
        INSERT INTO api_tokens (user_id, name, token_hash, prefix, expires_at)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING id, name, prefix, expires_at, last_used_at, revoked_at, created_at
        """,
        user_id,
        name,
        _hash(token),
        token[: len(PREFIX) + 6],
        expires,
    )
    return token, row


async def lookup(conn, token: str) -> Optional[Any]:
    """Active token's user (id, username, app_role) or None; records last use."""
    return await conn.fetchrow(
        """
        UPDATE api_tokens t SET last_used_at = NOW()
        FROM users u
        WHERE t.token_hash = $1 AND u.id = t.user_id AND u.is_active
          AND t.revoked_at IS NULL AND (t.expires_at IS NULL OR t.expires_at > NOW())
        RETURNING t.id AS token_id, u.id, u.username, u.app_role
        """,
        _hash(token),
    )


def serialize(row: Any) -> dict:
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "prefix": row["prefix"],
        "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
        "last_used_at": row["last_used_at"].isoformat() if row["last_used_at"] else None,
        "revoked": row["revoked_at"] is not None,
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }
