"""Notification emit helper.

``emit(conn, project_id, kind, payload, audience)``:
- ``audience='project'`` writes one row with ``user_id=NULL`` — visible to
  every Management role for the project.
- ``audience=[uuid, ...]`` fans out one row per user id (personal bell).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Iterable, Literal, Optional, Union
from uuid import UUID

log = logging.getLogger(__name__)

Audience = Union[Literal["project"], Iterable[Union[UUID, str]]]


async def emit(
    conn: Any,
    *,
    project_id: UUID | str,
    kind: str,
    payload: Optional[dict] = None,
    audience: Audience = "project",
) -> None:
    payload_json = json.dumps(payload or {})
    try:
        if audience == "project":
            await conn.execute(
                """
                INSERT INTO notifications (project_id, user_id, kind, payload)
                VALUES ($1, NULL, $2, $3::jsonb)
                """,
                project_id,
                kind,
                payload_json,
            )
            return
        for uid in audience:
            await conn.execute(
                """
                INSERT INTO notifications (project_id, user_id, kind, payload)
                VALUES ($1, $2, $3, $4::jsonb)
                """,
                project_id,
                uid,
                kind,
                payload_json,
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("notifications.emit failed (%s): %s", kind, exc)
