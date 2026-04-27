"""Issues API — list/create/patch + attach media.

Role rules:
- Artists can create issues only via the Command Palette (``source='palette'``
  in the body). Web-only creation is blocked at 403.
- Supervisor/Production create from anywhere.
- Pipeline has full access.

Attachments are stored in CAS; this router only records pointers
(``content_id``) into ``issue_attachments`` — uploads go through the existing
``/api/v1/cas`` endpoints.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path as _Path
from typing import Any, Optional
from urllib.parse import quote as _urlquote
from uuid import UUID

import asyncpg
from blake3 import blake3
from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Path, Query, UploadFile
from fastapi.responses import StreamingResponse

from app.auth.deps import CurrentUser, require_current_user
from app.cas.factory import get_cas_backend, is_cas_configured
from app.db import acquire
from app.events.log import emit as emit_event
from app.notifications.service import emit as emit_notification

router = APIRouter(prefix="/api/v1/issues", tags=["issues"])

_ISSUE_STATUSES = ("not_started", "in_progress", "testing", "closed")


def _normalize_status(value: Optional[str]) -> Optional[str]:
    """Coerce legacy 'unassigned' to 'not_started' so old palette clients keep working."""
    if value is None:
        return None
    lowered = str(value).strip().lower()
    if lowered == "unassigned":
        return "not_started"
    return lowered


def _parse_json(value: Any) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


# Columns the issue SELECTs below return. Kept as a module-level constant so
# list/get/create/update queries stay in sync with ``_serialize``.
_ISSUE_CORE_COLS = (
    "i.id, i.project_id, i.title, i.body, i.status, i.reporter_id, i.assignee_id, "
    "i.asset_id, i.shot_id, i.dcc, i.metadata, i.created_at, i.updated_at"
)

_ISSUE_LIST_SELECT = f"""
    SELECT {_ISSUE_CORE_COLS},
           ur.username AS reporter_username, ur.name AS reporter_name,
           ua.username AS assignee_username, ua.name AS assignee_name,
           a.code AS asset_code, a.name AS asset_name,
           sh.shot_code AS shot_code
    FROM issues i
    LEFT JOIN users ur  ON i.reporter_id = ur.id
    LEFT JOIN users ua  ON i.assignee_id = ua.id
    LEFT JOIN assets a  ON i.asset_id    = a.id
    LEFT JOIN shots  sh ON i.shot_id     = sh.id
"""


def _serialize(row) -> dict:
    out = {
        "id": str(row["id"]),
        "project_id": str(row["project_id"]),
        "title": row["title"],
        "body": row["body"],
        "status": row["status"],
        "reporter_id": str(row["reporter_id"]) if row["reporter_id"] else None,
        "assignee_id": str(row["assignee_id"]) if row["assignee_id"] else None,
        "asset_id": str(row["asset_id"]) if row.get("asset_id") else None,
        "shot_id": str(row["shot_id"]) if row.get("shot_id") else None,
        "dcc": row["dcc"],
        "metadata": _parse_json(row["metadata"]),
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }
    # Optional join columns — only present when queried via `_ISSUE_LIST_SELECT`.
    for key in (
        "reporter_username",
        "reporter_name",
        "assignee_username",
        "assignee_name",
        "asset_code",
        "asset_name",
        "shot_code",
    ):
        if key in row.keys():
            out[key] = row[key]
    # Convenient "entity" summary used by the UI card / detail.
    if out["asset_id"]:
        out["entity"] = {
            "type": "asset",
            "id": out["asset_id"],
            "code": out.get("asset_code"),
            "name": out.get("asset_name"),
        }
    elif out["shot_id"]:
        out["entity"] = {
            "type": "shot",
            "id": out["shot_id"],
            "code": out.get("shot_code"),
            "name": out.get("shot_code"),
        }
    else:
        out["entity"] = None
    return out


@router.get("")
async def list_issues(
    project_id: UUID = Query(...),
    status: Optional[str] = Query(None),
    # Sentinel ``none`` (not a UUID) means "filter where this column IS NULL",
    # e.g. "Unassigned" for assignee or "General" for dcc.
    assignee_id: Optional[str] = Query(None),
    reporter_id: Optional[UUID] = Query(None),
    dcc: Optional[str] = Query(None, description="Filter by DCC name. Use 'none' for issues with no DCC."),
    asset_id: Optional[UUID] = Query(None),
    shot_id: Optional[UUID] = Query(None),
    _user: CurrentUser = Depends(require_current_user),
) -> list[dict]:
    conds: list[str] = ["i.project_id = $1"]
    params: list[Any] = [project_id]
    if status:
        norm = _normalize_status(status)
        if norm not in _ISSUE_STATUSES:
            raise HTTPException(status_code=400, detail=f"status must be one of {_ISSUE_STATUSES}")
        conds.append(f"i.status = ${len(params) + 1}")
        params.append(norm)
    if assignee_id:
        token = str(assignee_id).strip().lower()
        if token == "none":
            conds.append("i.assignee_id IS NULL")
        else:
            try:
                uid = UUID(str(assignee_id))
            except (ValueError, TypeError) as exc:
                raise HTTPException(status_code=400, detail="assignee_id must be a UUID or 'none'") from exc
            conds.append(f"i.assignee_id = ${len(params) + 1}")
            params.append(uid)
    if reporter_id:
        conds.append(f"i.reporter_id = ${len(params) + 1}")
        params.append(reporter_id)
    if dcc:
        token = dcc.strip().lower()
        if token == "none":
            conds.append("i.dcc IS NULL")
        else:
            conds.append(f"LOWER(i.dcc) = ${len(params) + 1}")
            params.append(token)
    if asset_id:
        conds.append(f"i.asset_id = ${len(params) + 1}")
        params.append(asset_id)
    if shot_id:
        conds.append(f"i.shot_id = ${len(params) + 1}")
        params.append(shot_id)

    query = _ISSUE_LIST_SELECT + " WHERE " + " AND ".join(conds) + " ORDER BY i.created_at DESC"
    async with acquire() as conn:
        rows = await conn.fetch(query, *params)
    return [_serialize(r) for r in rows]


@router.get("/{issue_id}")
async def get_issue(
    issue_id: UUID = Path(...), _user: CurrentUser = Depends(require_current_user)
) -> dict:
    async with acquire() as conn:
        row = await conn.fetchrow(
            _ISSUE_LIST_SELECT + " WHERE i.id = $1",
            issue_id,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Issue not found")
        attachments = await conn.fetch(
            """
            SELECT id, issue_id, content_id, filename, mime_type, size_bytes, uploaded_by, created_at
            FROM issue_attachments
            WHERE issue_id = $1
            ORDER BY created_at
            """,
            issue_id,
        )
    data = _serialize(row)
    data["attachments"] = [
        {
            "id": str(a["id"]),
            "issue_id": str(a["issue_id"]),
            "content_id": a["content_id"],
            "filename": a["filename"],
            "mime_type": a["mime_type"],
            "size_bytes": int(a["size_bytes"]) if a["size_bytes"] is not None else None,
            "uploaded_by": str(a["uploaded_by"]) if a["uploaded_by"] else None,
            "created_at": a["created_at"].isoformat() if a["created_at"] else None,
        }
        for a in attachments
    ]
    return data


@router.post("")
async def create_issue(
    body: dict = Body(...), user: CurrentUser = Depends(require_current_user)
) -> dict:
    """Create an issue. Role rules enforced here.

    Artists may only create via the Command Palette; the body must carry
    ``source='palette'``. The caller's username is recorded as reporter.
    """
    title = (body.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title is required")
    project_id = body.get("project_id")
    project_code = (body.get("project_code") or "").strip() or None
    asset_code = (body.get("asset_code") or "").strip() or None
    shot_code = (body.get("shot_code") or "").strip() or None

    # Palette clients know project/asset by *code* not UUID. Resolve here so
    # DCC callers can stay stateless.
    if not project_id and project_code:
        async with acquire() as conn:
            pid = await conn.fetchval(
                "SELECT id FROM projects WHERE code = $1", project_code
            )
        if pid:
            project_id = str(pid)
    if not project_id:
        raise HTTPException(
            status_code=400, detail="project_id (or project_code) is required"
        )

    role = (user.app_role or "").lower()
    source = (body.get("source") or "web").lower()
    if role == "artist" and source != "palette":
        raise HTTPException(
            status_code=403,
            detail="Artists may only create issues via the Command Palette",
        )
    if role not in ("artist", "pipeline", "supervisor", "production"):
        raise HTTPException(status_code=403, detail="Role cannot create issues")

    issue_body = body.get("body")
    status = _normalize_status(body.get("status", "not_started"))
    if status not in _ISSUE_STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of {_ISSUE_STATUSES}")
    reporter_id = body.get("reporter_id") or user.id
    assignee_id = body.get("assignee_id")
    asset_id = body.get("asset_id")
    shot_id = body.get("shot_id")
    if asset_id and shot_id:
        raise HTTPException(
            status_code=400,
            detail="Issue can be linked to either asset_id or shot_id, not both",
        )
    dcc = body.get("dcc")
    metadata = body.get("metadata") or {}

    # Resolve asset/shot by code (scoped to the project) if caller only has codes.
    if not asset_id and asset_code:
        async with acquire() as conn:
            aid = await conn.fetchval(
                "SELECT id FROM assets WHERE project_id = $1 AND code = $2",
                project_id,
                asset_code,
            )
        if aid:
            asset_id = str(aid)
    if not shot_id and shot_code:
        async with acquire() as conn:
            sid = await conn.fetchval(
                "SELECT id FROM shots WHERE project_id = $1 AND shot_code = $2",
                project_id,
                shot_code,
            )
        if sid:
            shot_id = str(sid)

    async with acquire() as conn:
        try:
            inserted = await conn.fetchrow(
                """
                INSERT INTO issues (project_id, title, body, status, reporter_id, assignee_id,
                                    asset_id, shot_id, dcc, metadata)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb)
                RETURNING id
                """,
                project_id,
                title,
                issue_body,
                status,
                reporter_id,
                assignee_id,
                asset_id,
                shot_id,
                dcc,
                json.dumps(metadata),
            )
        except asyncpg.ForeignKeyViolationError as exc:
            raise HTTPException(
                status_code=400, detail="project_id/asset_id/shot_id/assignee_id invalid"
            ) from exc
        row = await conn.fetchrow(_ISSUE_LIST_SELECT + " WHERE i.id = $1", inserted["id"])

        await emit_event(
            conn,
            project_id=row["project_id"],
            actor_id=user.id if user.id else None,
            kind="issue.created",
            payload={
                "issue_id": str(row["id"]),
                "title": row["title"],
                "dcc": row["dcc"],
                "source": source,
            },
        )
        await emit_notification(
            conn,
            project_id=row["project_id"],
            kind="issue.created",
            payload={
                "issue_id": str(row["id"]),
                "title": row["title"],
                "reporter_id": str(row["reporter_id"]) if row["reporter_id"] else None,
            },
            audience="project",
        )
    return _serialize(row)


@router.patch("/{issue_id}")
async def update_issue(
    issue_id: UUID,
    body: dict = Body(...),
    user: CurrentUser = Depends(require_current_user),
) -> dict:
    allowed = ("title", "body", "status", "assignee_id", "asset_id", "shot_id", "dcc")
    fields: list[str] = []
    params: list[Any] = []
    for key in allowed:
        if key in body:
            value = body[key]
            if key == "status":
                value = _normalize_status(value)
                if value not in _ISSUE_STATUSES:
                    raise HTTPException(
                        status_code=400, detail=f"status must be one of {_ISSUE_STATUSES}"
                    )
            fields.append(f"{key} = ${len(params) + 1}")
            params.append(value)
    if "metadata" in body:
        fields.append(f"metadata = ${len(params) + 1}::jsonb")
        params.append(json.dumps(body["metadata"] or {}))
    if not fields:
        raise HTTPException(status_code=400, detail="No updatable fields provided")

    params.append(issue_id)
    update_sql = (
        "UPDATE issues SET "
        + ", ".join(fields)
        + " WHERE id = $"
        + str(len(params))
        + " RETURNING id"
    )
    async with acquire() as conn:
        updated = await conn.fetchrow(update_sql, *params)
        if not updated:
            raise HTTPException(status_code=404, detail="Issue not found")
        row = await conn.fetchrow(_ISSUE_LIST_SELECT + " WHERE i.id = $1", updated["id"])
        await emit_event(
            conn,
            project_id=row["project_id"],
            actor_id=user.id,
            kind="issue.updated",
            payload={"issue_id": str(row["id"]), "status": row["status"]},
        )
        if row["status"] == "closed":
            await emit_notification(
                conn,
                project_id=row["project_id"],
                kind="issue.closed",
                payload={"issue_id": str(row["id"]), "title": row["title"]},
                audience="project",
            )
    return _serialize(row)


@router.post("/{issue_id}/attachments")
async def attach_media(
    issue_id: UUID,
    body: dict = Body(...),
    user: CurrentUser = Depends(require_current_user),
) -> dict:
    """Record an attachment already uploaded to CAS (content_id mandatory)."""
    content_id = (body.get("content_id") or "").strip()
    filename = (body.get("filename") or "").strip()
    if not content_id or not filename:
        raise HTTPException(status_code=400, detail="content_id and filename are required")
    mime_type = body.get("mime_type")
    size_bytes = body.get("size_bytes")

    async with acquire() as conn:
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO issue_attachments (issue_id, content_id, filename, mime_type, size_bytes, uploaded_by)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING id, issue_id, content_id, filename, mime_type, size_bytes, uploaded_by, created_at
                """,
                issue_id,
                content_id,
                filename,
                mime_type,
                size_bytes,
                user.id,
            )
        except asyncpg.ForeignKeyViolationError as exc:
            raise HTTPException(status_code=404, detail="Issue not found") from exc
    return _serialize_attachment(row)


def _serialize_attachment(row: asyncpg.Record) -> dict:
    return {
        "id": str(row["id"]),
        "issue_id": str(row["issue_id"]),
        "content_id": row["content_id"],
        "filename": row["filename"],
        "mime_type": row["mime_type"],
        "size_bytes": int(row["size_bytes"]) if row["size_bytes"] is not None else None,
        "uploaded_by": str(row["uploaded_by"]) if row["uploaded_by"] else None,
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


# Max per-file upload size to stop runaway uploads eating disk. 100 MiB is a
# generous fit for screenshots / short clips; tune via env if a studio needs more.
_MAX_UPLOAD_BYTES = int(os.environ.get("ZENO_ISSUE_UPLOAD_MAX_BYTES", str(100 * 1024 * 1024)))


@router.post("/{issue_id}/attachments/upload")
async def upload_attachment(
    issue_id: UUID,
    file: UploadFile = File(...),
    filename: Optional[str] = Form(None),
    user: CurrentUser = Depends(require_current_user),
) -> dict:
    """Multipart upload: streams the file into CAS, then records the pointer.

    Unlike ``POST /attachments`` (which expects a pre-computed ``content_id``),
    this endpoint is what the web UI calls — it handles the BLAKE3 hashing and
    CAS write server-side so the browser never has to do blob dedup itself.
    """
    if not is_cas_configured():
        raise HTTPException(status_code=503, detail="CAS not configured")
    try:
        backend = get_cas_backend()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    async with acquire() as conn:
        exists = await conn.fetchval("SELECT 1 FROM issues WHERE id = $1", issue_id)
    if not exists:
        raise HTTPException(status_code=404, detail="Issue not found")

    tmp_dir = backend.ensure_tmp()
    hasher = blake3()
    fd, tmp_path = tempfile.mkstemp(dir=tmp_dir, prefix="issue_")
    written = 0
    try:
        with os.fdopen(fd, "wb") as f:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > _MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="File too large")
                hasher.update(chunk)
                f.write(chunk)
        content_id = hasher.hexdigest()
        backend.put_from_path(content_id, _Path(tmp_path))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Upload failed: {exc}") from exc
    finally:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    resolved_name = (filename or file.filename or "attachment").strip() or "attachment"
    mime_type = file.content_type or "application/octet-stream"

    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO issue_attachments (issue_id, content_id, filename, mime_type, size_bytes, uploaded_by)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id, issue_id, content_id, filename, mime_type, size_bytes, uploaded_by, created_at
            """,
            issue_id,
            content_id,
            resolved_name,
            mime_type,
            written,
            user.id,
        )
    return _serialize_attachment(row)


@router.get("/{issue_id}/attachments/{attachment_id}")
async def download_attachment(
    issue_id: UUID,
    attachment_id: UUID,
    _user: CurrentUser = Depends(require_current_user),
):
    """Stream an attachment back to the client with a proper filename/mime."""
    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, issue_id, content_id, filename, mime_type, size_bytes
            FROM issue_attachments
            WHERE id = $1 AND issue_id = $2
            """,
            attachment_id,
            issue_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Attachment not found")
    if not is_cas_configured():
        raise HTTPException(status_code=503, detail="CAS not configured")
    try:
        backend = get_cas_backend()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    content_id = row["content_id"]
    if not backend.exists(content_id):
        raise HTTPException(status_code=404, detail="Blob missing from CAS")

    filename = row["filename"] or "attachment"
    mime_type = row["mime_type"] or "application/octet-stream"
    headers = {
        "Content-Disposition": (
            # RFC 5987 encoded filename* keeps non-ASCII names working in all browsers.
            f"inline; filename=\"{filename}\"; filename*=UTF-8''{_urlquote(filename)}"
        ),
        "Cache-Control": "private, max-age=300",
    }
    size = row["size_bytes"]
    if size is not None:
        headers["Content-Length"] = str(int(size))

    def gen():
        yield from backend.get_stream(content_id)

    return StreamingResponse(gen(), media_type=mime_type, headers=headers)
