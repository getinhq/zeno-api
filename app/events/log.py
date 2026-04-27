"""Lightweight activity-log writer.

Every domain write that should surface on the dashboard's Recent Activity
column goes through ``emit()``. Failures are swallowed (logged): we never
want a ``tasks`` INSERT to roll back because events is down/missing.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional
from uuid import UUID

import asyncpg

log = logging.getLogger(__name__)


async def emit(
    conn_or_pool: Any,
    *,
    project_id: Optional[UUID | str],
    actor_id: Optional[UUID | str],
    kind: str,
    payload: Optional[dict] = None,
) -> None:
    """Insert a row into ``events``. Errors are logged and swallowed."""
    try:
        payload_json = json.dumps(payload or {})
        # Support either a raw connection or an asyncpg pool.
        if hasattr(conn_or_pool, "execute"):
            executor = conn_or_pool
        else:
            executor = conn_or_pool
        await executor.execute(
            """
            INSERT INTO events (project_id, actor_id, kind, payload)
            VALUES ($1, $2, $3, $4::jsonb)
            """,
            project_id,
            actor_id,
            kind,
            payload_json,
        )
    except asyncpg.PostgresError as exc:
        log.warning("events.emit failed (%s): %s", kind, exc)
    except Exception as exc:  # noqa: BLE001
        log.warning("events.emit unexpected (%s): %s", kind, exc)
