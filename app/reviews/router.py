"""Review: submit media for a task, review it frame by frame, and carry notes to the fix.

Flow:

1. An artist submits media for a task (``POST /items``, from the dashboard or Blender).
   It becomes revision N of that task, the task moves to *review*, and the reviewer is
   notified. The file is converted in the background (see ``processing.py``).
2. Reviewers leave notes pinned to scene frames, optionally with a drawing. Notes belong
   to the task, not to one revision, so they stay visible on every later revision.
3. The artist marks a note *fixed*; the reviewer *verifies* it or reopens it.
4. A supervisor/producer decides: **approve** (task done, linked version approved, fixed
   notes verified) or **request changes** (task back in progress, artist notified).
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from fractions import Fraction
from pathlib import Path as _Path
from typing import Any, Optional
from uuid import UUID

from blake3 import blake3
from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Query, Response, UploadFile

from app.auth.deps import MANAGEMENT_ROLES, CurrentUser, require_current_user, require_management
from app.cas.factory import get_cas_backend, is_cas_configured
from app.db import acquire
from app.events.log import emit as emit_event
from app.notifications.service import emit as emit_notification
from app.reviews import processing
from app.reviews.media_urls import sign
from app.storage_policy.policy import resolve as resolve_storage
from app.storage_policy.router import load_for_project as load_policy_for_project
from app.tenancy import ensure_in_scope, org_filter_sql

router = APIRouter(prefix="/api/v1/reviews", tags=["reviews"])

MAX_UPLOAD_BYTES = int(os.environ.get("ZENO_REVIEW_MAX_UPLOAD_MB", "4096")) * 1024 * 1024
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".mxf"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".exr", ".dpx", ".tga", ".webp", ".bmp"}
NOTE_STATUSES = ("open", "fixed", "verified")
MAX_NOTE_CHARS = 5000
MAX_DRAWING_BYTES = 256 * 1024

# --- helpers ---------------------------------------------------------------------------

_ITEM_SELECT = """
    SELECT ri.*, t.title AS task_title, t.type AS task_type, t.status AS task_status,
           t.shot_id, t.asset_id, t.reviewer_id,
           s.shot_code, sq.code AS sequence_code, a.name AS asset_name, a.code AS asset_code,
           COALESCE(u.name, u.username) AS submitted_by_name,
           COALESCE(d.name, d.username) AS decided_by_name,
           (SELECT count(*) FROM review_notes n
             WHERE n.task_id = ri.task_id AND n.parent_id IS NULL AND n.status = 'open') AS open_notes,
           (SELECT count(*) FROM review_notes n
             WHERE n.task_id = ri.task_id AND n.parent_id IS NULL AND n.status = 'fixed') AS fixed_notes
    FROM review_items ri
    JOIN tasks t          ON t.id = ri.task_id
    JOIN projects p       ON p.id = ri.project_id
    LEFT JOIN shots s     ON s.id = t.shot_id
    LEFT JOIN sequences sq ON sq.id = s.sequence_id
    LEFT JOIN assets a    ON a.id = t.asset_id
    LEFT JOIN users u     ON u.id = ri.submitted_by
    LEFT JOIN users d     ON d.id = ri.decided_by
"""


def _is_management(user: Optional[CurrentUser]) -> bool:
    import app.config as app_config

    if not app_config.ENABLE_AUTH and (user is None or user.is_anonymous):
        return True
    return bool(user and (user.app_role or "").lower() in MANAGEMENT_ROLES)


def _iso(value) -> Optional[str]:
    return value.isoformat() if value else None


def _fps(row) -> Optional[float]:
    if row["fps_num"] and row["fps_den"]:
        return row["fps_num"] / row["fps_den"]
    return None


def serialize_item(row, *, urls: bool = True) -> dict:
    entity = row["shot_code"] or row["asset_name"]
    label = " / ".join(x for x in (row["sequence_code"], row["shot_code"]) if x) if row["shot_code"] else (row["asset_name"] or "")
    out = {
        "id": str(row["id"]),
        "project_id": str(row["project_id"]),
        "task_id": str(row["task_id"]),
        "task": {
            "id": str(row["task_id"]),
            "title": row["task_title"],
            "type": row["task_type"],
            "status": row["task_status"],
            "shot_id": str(row["shot_id"]) if row["shot_id"] else None,
            "asset_id": str(row["asset_id"]) if row["asset_id"] else None,
            "entity": entity,
            "entity_label": label,
        },
        "revision": row["revision"],
        "label": f"v{row['revision']:03d}",
        "version_id": str(row["version_id"]) if row["version_id"] else None,
        "description": row["description"],
        "kind": row["kind"],
        "source_filename": row["source_filename"],
        "source_size": row["source_size"],
        "fps": _fps(row),
        "fps_num": row["fps_num"],
        "fps_den": row["fps_den"],
        "frame_start": row["frame_start"],
        "frame_count": row["frame_count"],
        "frame_end": (row["frame_start"] + row["frame_count"] - 1) if row["frame_count"] else None,
        "width": row["width"],
        "height": row["height"],
        "has_audio": row["has_audio"],
        "source_info": row["source_info"] if isinstance(row["source_info"], dict) else json.loads(row["source_info"] or "{}"),
        "sequence": (
            (row["source_info"] if isinstance(row["source_info"], dict) else json.loads(row["source_info"] or "{}")).get("sequence")
            if row["kind"] == "sequence"
            else None
        ),
        "processing_status": row["processing_status"],
        "processing_error": row["processing_error"],
        "review_status": row["review_status"],
        "decided_by": row["decided_by_name"],
        "decided_at": _iso(row["decided_at"]),
        "submitted_by": str(row["submitted_by"]) if row["submitted_by"] else None,
        "submitted_by_name": row["submitted_by_name"],
        "created_at": _iso(row["created_at"]),
        "open_notes": int(row["open_notes"] or 0),
        "fixed_notes": int(row["fixed_notes"] or 0),
        "filmstrip_tiles": row["filmstrip_tiles"],
        "filmstrip_step": row["filmstrip_step"],
    }
    if urls:
        ready = row["processing_status"] == "ready"
        out["media"] = {
            "proxy": sign(row["proxy_content_id"], row["proxy_mime"] or "video/mp4") if ready else None,
            "thumbnail": sign(row["thumb_content_id"], "image/jpeg"),
            "filmstrip": sign(row["filmstrip_content_id"], "image/jpeg"),
            # A sequence's source is a frame manifest, not something to download or open.
            "original": None
            if row["kind"] == "sequence"
            else sign(row["source_content_id"], "application/octet-stream", filename=row["source_filename"]),
        }
    return out


def _note_out(row, revisions: dict) -> dict:
    return {
        "id": str(row["id"]),
        "task_id": str(row["task_id"]),
        "item_id": str(row["item_id"]),
        "raised_in": revisions.get(row["item_id"]),
        "parent_id": str(row["parent_id"]) if row["parent_id"] else None,
        "kind": row["kind"],
        "body": row["body"],
        "frame": row["frame"],
        "frame_end": row["frame_end"],
        "drawing": row["drawing"] if not isinstance(row["drawing"], str) else json.loads(row["drawing"]),
        "status": row["status"],
        "fixed_in_item_id": str(row["fixed_in_item_id"]) if row["fixed_in_item_id"] else None,
        "fixed_in": revisions.get(row["fixed_in_item_id"]),
        "visibility": row["visibility"],
        "author_id": str(row["author_id"]) if row["author_id"] else None,
        "author_name": row["author_name"] or row["guest_name"] or "Someone",
        "is_guest": row["author_id"] is None and bool(row["guest_name"]),
        "meta": row["meta"] if isinstance(row["meta"], dict) else json.loads(row["meta"] or "{}"),
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


async def load_threads(conn, task_id: UUID, *, client_only: bool = False) -> list[dict]:
    """Top-level notes of a task, each with its replies/status history, oldest first."""
    revisions = {
        r["id"]: f"v{r['revision']:03d}"
        for r in await conn.fetch("SELECT id, revision FROM review_items WHERE task_id = $1", task_id)
    }
    rows = await conn.fetch(
        f"""
        SELECT n.*, COALESCE(u.name, u.username) AS author_name
        FROM review_notes n
        LEFT JOIN users u ON u.id = n.author_id
        WHERE n.task_id = $1 {"AND n.visibility = 'client'" if client_only else ""}
        ORDER BY n.created_at, n.id
        """,
        task_id,
    )
    threads: dict[UUID, dict] = {}
    children: list = []
    for r in rows:
        if r["parent_id"] is None:
            threads[r["id"]] = {**_note_out(r, revisions), "replies": []}
        else:
            children.append(r)
    for r in children:
        parent = threads.get(r["parent_id"])
        if parent is not None:
            parent["replies"].append(_note_out(r, revisions))
    return list(threads.values())


async def _fetch_item(conn, item_id: UUID):
    await ensure_in_scope(conn, "review_item", item_id)
    row = await conn.fetchrow(_ITEM_SELECT + " WHERE ri.id = $1", item_id)
    if not row:
        raise HTTPException(status_code=404, detail="Review item not found")
    return row


async def task_people(conn, task_id: UUID) -> list[UUID]:
    rows = await conn.fetch(
        """
        SELECT assignee_id AS uid FROM tasks WHERE id = $1 AND assignee_id IS NOT NULL
        UNION SELECT user_id FROM task_assignees WHERE task_id = $1
        """,
        task_id,
    )
    return [r["uid"] for r in rows]


def _parse_fps(value: Optional[str]) -> Optional[Fraction]:
    if value in (None, ""):
        return None
    try:
        fps = Fraction(str(value).strip()).limit_denominator(1001)
    except (ValueError, ZeroDivisionError):
        raise HTTPException(status_code=400, detail="fps must be a number like 24 or 24000/1001") from None
    if not 1 <= fps <= 240:
        raise HTTPException(status_code=400, detail="fps must be between 1 and 240")
    return fps


def _validate_drawing(drawing: Any) -> Optional[str]:
    if drawing in (None, {}, []):
        return None
    if not isinstance(drawing, dict) or not isinstance(drawing.get("shapes"), list):
        raise HTTPException(status_code=400, detail="drawing must be {\"shapes\": [...]}")
    raw = json.dumps(drawing)
    if len(raw) > MAX_DRAWING_BYTES:
        raise HTTPException(status_code=413, detail="Drawing is too large")
    return raw


def _clean_body(text: Optional[str], *, required: bool) -> str:
    body = (text or "").strip()
    if required and not body:
        raise HTTPException(status_code=400, detail="Write something first")
    if len(body) > MAX_NOTE_CHARS:
        raise HTTPException(status_code=400, detail=f"Notes are limited to {MAX_NOTE_CHARS} characters")
    return body


async def stream_upload_to_cas(file: UploadFile) -> tuple[str, int]:
    if not is_cas_configured():
        raise HTTPException(status_code=503, detail="File storage is not configured")
    backend = get_cas_backend()
    hasher = blake3()
    fd, tmp_path = tempfile.mkstemp(dir=backend.ensure_tmp(), prefix="review_")
    written = 0
    try:
        with os.fdopen(fd, "wb") as f:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="File is too large")
                hasher.update(chunk)
                f.write(chunk)
        if written == 0:
            raise HTTPException(status_code=400, detail="The file is empty")
        content_id = hasher.hexdigest()
        backend.put_from_path(content_id, _Path(tmp_path))
        return content_id, written
    finally:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# --- submissions -----------------------------------------------------------------------


@router.post("/items", status_code=201)
async def submit_for_review(
    task_id: UUID = Form(...),
    file: UploadFile = File(...),
    frame_start: Optional[int] = Form(None),
    fps: Optional[str] = Form(None),
    description: Optional[str] = Form(None),
    version_id: Optional[UUID] = Form(None),
    user: CurrentUser = Depends(require_current_user),
) -> dict:
    """Upload a playblast/render/still for a task. Becomes the task's next revision."""
    filename = (file.filename or "media").strip() or "media"
    ext = _Path(filename).suffix.lower()
    if ext not in VIDEO_EXTS | IMAGE_EXTS:
        raise HTTPException(
            status_code=415,
            detail="Upload a video (MP4, MOV, MKV, WebM, AVI, MXF) or an image (PNG, JPEG, TIFF, EXR, DPX)",
        )
    rate = _parse_fps(fps)
    if frame_start is not None and not -100000 <= frame_start <= 10_000_000:
        raise HTTPException(status_code=400, detail="frame_start is out of range")

    task = await _load_task(task_id, version_id)
    content_id, size = await stream_upload_to_cas(file)
    return await _create_item(
        task,
        user,
        version_id=version_id,
        description=description,
        kind="image" if ext in IMAGE_EXTS else "video",
        source_content_id=content_id,
        source_filename=filename,
        source_size=size,
        rate=rate,
        frame_start=frame_start if frame_start is not None else 1,
    )


async def _load_task(task_id: UUID, version_id: Optional[UUID]):
    async with acquire() as conn:
        await ensure_in_scope(conn, "task", task_id)
        task = await conn.fetchrow(
            "SELECT t.id, t.project_id, t.reviewer_id, t.title, t.type FROM tasks t WHERE t.id = $1",
            task_id,
        )
        if not task or not task["project_id"]:
            raise HTTPException(status_code=404, detail="Task not found")
        if version_id:
            await ensure_in_scope(conn, "version", version_id)
    return task


async def _create_item(
    task,
    user: CurrentUser,
    *,
    version_id: Optional[UUID],
    description: Optional[str],
    kind: str,
    source_content_id: str,
    source_filename: str,
    source_size: int,
    rate: Optional[Fraction],
    frame_start: int,
    frames: Optional[list] = None,
    source_info: Optional[dict] = None,
) -> dict:
    """Insert the task's next revision, move the task to review, notify, start conversion."""
    task_id = task["id"]
    async with acquire() as conn:
        async with conn.transaction():
            # Serialize revision numbering per task.
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", f"review:{task_id}")
            revision = await conn.fetchval(
                "SELECT COALESCE(MAX(revision), 0) + 1 FROM review_items WHERE task_id = $1", task_id
            )
            item_id = await conn.fetchval(
                """
                INSERT INTO review_items (project_id, task_id, revision, version_id, description,
                    kind, source_content_id, source_filename, source_size, fps_num, fps_den,
                    frame_start, submitted_by, frames, source_info)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14::jsonb,
                        COALESCE($15::jsonb, '{}'::jsonb))
                RETURNING id
                """,
                task["project_id"],
                task_id,
                revision,
                version_id,
                _clean_body(description, required=False) or None,
                kind,
                source_content_id,
                source_filename,
                source_size,
                rate.numerator if rate else None,
                rate.denominator if rate else None,
                frame_start,
                user.db_id,
                json.dumps(frames) if frames is not None else None,
                json.dumps(source_info) if source_info is not None else None,
            )
            await conn.execute(
                "UPDATE tasks SET status = 'review', updated_at = NOW() WHERE id = $1 AND status <> 'review'",
                task_id,
            )
            if version_id:
                await conn.execute(
                    "UPDATE versions SET status = 'in_review' WHERE id = $1 AND status = 'pending'", version_id
                )
            payload = {
                "item_id": str(item_id),
                "task_id": str(task_id),
                "task_title": task["title"],
                "revision": f"v{revision:03d}",
            }
            await emit_event(conn, project_id=task["project_id"], actor_id=user.db_id, kind="review.submitted", payload=payload)
            await emit_notification(
                conn,
                project_id=task["project_id"],
                kind="review.submitted",
                payload=payload,
                audience=[task["reviewer_id"]] if task["reviewer_id"] else "project",
            )
        row = await conn.fetchrow(_ITEM_SELECT + " WHERE ri.id = $1", item_id)
    processing.enqueue(item_id)
    return serialize_item(row)


# --- image sequences -------------------------------------------------------------------


async def ensure_sequences_allowed(project_id, ext: str) -> None:
    """Studio storage workflow can switch per-frame sequences off (per format or entirely)."""
    async with acquire() as conn:
        stored = await load_policy_for_project(conn, project_id)
    decision = resolve_storage(stored, ext)
    if decision["strategy"] == "off":
        raise HTTPException(
            status_code=403,
            detail=f"Image sequences are turned off for .{decision['extension']} in Settings → Storage workflow "
            f"({decision['reason']}) Submit a movie instead.",
        )

MAX_SEQUENCE_FRAMES = 20000
_FRAME_NUMBER_RE = re.compile(r"(\d+)(?=\.[A-Za-z0-9]+$)")


def frame_number(filename: str) -> int:
    """Frame number from a sequence file name: ``sh010.1012.exr`` / ``frame_1012.png`` -> 1012."""
    m = None
    for m in _FRAME_NUMBER_RE.finditer(_Path(filename).name):
        pass
    if m is None:
        raise HTTPException(status_code=400, detail=f"No frame number in file name: {filename}")
    return int(m.group(1))


def sequence_pattern(filename: str) -> str:
    """``sh010.1012.exr`` -> ``sh010.####.exr`` (for display)."""
    name = _Path(filename).name
    return _FRAME_NUMBER_RE.sub(lambda m: "#" * len(m.group(1)), name)


async def _create_sequence(
    task,
    user: CurrentUser,
    frames: list[dict],
    *,
    rate: Optional[Fraction],
    description: Optional[str],
    version_id: Optional[UUID],
) -> dict:
    """``frames``: [{frame, content_id, size, filename}] already in CAS (one blob per frame)."""
    if not frames:
        raise HTTPException(status_code=400, detail="The sequence has no frames")
    if len(frames) > MAX_SEQUENCE_FRAMES:
        raise HTTPException(status_code=400, detail=f"Sequences are limited to {MAX_SEQUENCE_FRAMES} frames")
    frames = sorted(frames, key=lambda f: f["frame"])
    numbers = [f["frame"] for f in frames]
    if len(set(numbers)) != len(numbers):
        raise HTTPException(status_code=400, detail="Two files have the same frame number")
    ext = _Path(frames[0]["filename"]).suffix.lower()
    if ext not in IMAGE_EXTS or any(_Path(f["filename"]).suffix.lower() != ext for f in frames):
        raise HTTPException(status_code=415, detail="A sequence is one image format (EXR, PNG, TIFF, JPEG, DPX…)")
    await ensure_sequences_allowed(task["project_id"], ext)

    # What this revision really adds: frames whose bytes no earlier revision of the task stored.
    async with acquire() as conn:
        earlier = await conn.fetch(
            "SELECT revision, frames FROM review_items WHERE task_id = $1 AND frames IS NOT NULL", task["id"]
        )
    known: dict[str, str] = {}
    for r in sorted(earlier, key=lambda r: r["revision"]):
        for f in r["frames"] if isinstance(r["frames"], list) else json.loads(r["frames"] or "[]"):
            known.setdefault(f["content_id"], f"v{r['revision']:03d}")
    total = sum(int(f["size"]) for f in frames)
    unique = {f["content_id"]: int(f["size"]) for f in frames}
    reused = [f for f in frames if f["content_id"] in known]
    new_bytes = sum(size for cid, size in unique.items() if cid not in known)
    stats = {
        "ext": ext,
        "pattern": sequence_pattern(frames[0]["filename"]),
        "frames": len(frames),
        "first": numbers[0],
        "last": numbers[-1],
        "total_bytes": total,
        "unique_frames": len(unique),
        "reused_frames": len(reused),
        "reused_bytes": sum(int(f["size"]) for f in reused),
        "reused_from": sorted({known[f["content_id"]] for f in reused}),
        "new_bytes": new_bytes,
        "saved_ratio": round(1 - new_bytes / total, 4) if total else 0.0,
    }
    manifest = [{"frame": f["frame"], "content_id": f["content_id"], "size": int(f["size"])} for f in frames]
    raw = json.dumps({"version": 1, "pattern": stats["pattern"], "frames": manifest}, separators=(",", ":")).encode()
    manifest_id = blake3(raw).hexdigest()
    backend = get_cas_backend()
    if not backend.exists(manifest_id):
        fd, tmp = tempfile.mkstemp(dir=backend.ensure_tmp(), prefix="manifest_")
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        backend.put_from_path(manifest_id, _Path(tmp))
        if os.path.exists(tmp):
            os.unlink(tmp)
    return await _create_item(
        task,
        user,
        version_id=version_id,
        description=description,
        kind="sequence",
        source_content_id=manifest_id,
        source_filename=f"{stats['pattern']} ({len(frames)} frames)",
        source_size=total,
        rate=rate,
        frame_start=numbers[0],
        frames=manifest,
        source_info={"sequence": stats},
    )


@router.post("/items/sequence", status_code=201)
async def submit_sequence(body: dict = Body(...), user: CurrentUser = Depends(require_current_user)) -> dict:
    """Frames already uploaded to CAS (``PUT /api/v1/cas/blobs/{hash}``, skipping ones that exist).

    Body: ``task_id``, ``frames: [{frame, content_id, size, filename}]``, optional ``fps``,
    ``description``, ``version_id``. DCC plugins use this so unchanged frames are never re-sent.
    """
    try:
        task_id = UUID(str(body.get("task_id")))
        version_id = UUID(str(body["version_id"])) if body.get("version_id") else None
    except ValueError:
        raise HTTPException(status_code=400, detail="task_id must be a task id") from None
    raw_frames = body.get("frames")
    if not isinstance(raw_frames, list):
        raise HTTPException(status_code=400, detail="frames must be a list")
    backend = get_cas_backend() if is_cas_configured() else None
    if backend is None:
        raise HTTPException(status_code=503, detail="File storage is not configured")
    frames = []
    for f in raw_frames:
        try:
            item = {
                "frame": int(f["frame"]),
                "content_id": str(f["content_id"]).lower(),
                "size": int(f["size"]),
                "filename": str(f.get("filename") or ""),
            }
        except (KeyError, TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Each frame needs frame, content_id, size, filename") from None
        frames.append(item)
    missing = [f["frame"] for f in frames if not backend.exists(f["content_id"])]
    if missing:
        raise HTTPException(status_code=409, detail=f"{len(missing)} frame(s) not uploaded yet, e.g. {missing[:5]}")
    task = await _load_task(task_id, version_id)
    return await _create_sequence(
        task, user, frames, rate=_parse_fps(body.get("fps")), description=body.get("description"), version_id=version_id
    )


@router.post("/items/sequence/upload", status_code=201)
async def upload_sequence(
    task_id: UUID = Form(...),
    files: list[UploadFile] = File(...),
    fps: Optional[str] = Form(None),
    description: Optional[str] = Form(None),
    version_id: Optional[UUID] = Form(None),
    user: CurrentUser = Depends(require_current_user),
) -> dict:
    """Browser upload of a whole sequence (multiple files). Each frame is stored once by hash."""
    if len(files) > MAX_SEQUENCE_FRAMES:
        raise HTTPException(status_code=400, detail=f"Sequences are limited to {MAX_SEQUENCE_FRAMES} frames")
    task = await _load_task(task_id, version_id)
    if files:
        # Refuse before storing any frame when the studio has sequences off for this format.
        await ensure_sequences_allowed(task["project_id"], _Path(files[0].filename or "").suffix.lower())
    rate = _parse_fps(fps)
    frames = []
    for f in files:
        name = (f.filename or "").strip()
        number = frame_number(name)
        content_id, size = await stream_upload_to_cas(f)
        frames.append({"frame": number, "content_id": content_id, "size": size, "filename": name})
    return await _create_sequence(task, user, frames, rate=rate, description=description, version_id=version_id)


@router.get("/items")
async def list_items(
    project_id: Optional[UUID] = Query(None),
    task_id: Optional[UUID] = Query(None),
    shot_id: Optional[UUID] = Query(None),
    asset_id: Optional[UUID] = Query(None),
    review_status: Optional[str] = Query(None, description="pending | approved | changes_requested"),
    latest_only: bool = Query(False, description="Only the newest revision of each task"),
    limit: int = Query(200, ge=1, le=500),
) -> list[dict]:
    if not (project_id or task_id or shot_id or asset_id):
        raise HTTPException(status_code=400, detail="Pass project_id, task_id, shot_id or asset_id")
    clauses, params = [], []

    def add(sql: str, value: Any) -> None:
        params.append(value)
        clauses.append(sql.replace("$?", f"${len(params)}"))

    async with acquire() as conn:
        if project_id:
            await ensure_in_scope(conn, "project", project_id)
            add("ri.project_id = $?", project_id)
        if task_id:
            await ensure_in_scope(conn, "task", task_id)
            add("ri.task_id = $?", task_id)
        if shot_id:
            await ensure_in_scope(conn, "shot", shot_id)
            add("t.shot_id = $?", shot_id)
        if asset_id:
            await ensure_in_scope(conn, "asset", asset_id)
            add("t.asset_id = $?", asset_id)
        if review_status:
            add("ri.review_status = $?", review_status)
        if latest_only:
            clauses.append("ri.revision = (SELECT MAX(r2.revision) FROM review_items r2 WHERE r2.task_id = ri.task_id)")
        org_sql, org_params = org_filter_sql("p.organization_id", len(params) + 1)
        clauses.append(org_sql)
        params += org_params
        params.append(limit)
        rows = await conn.fetch(
            _ITEM_SELECT + f" WHERE {' AND '.join(clauses)} ORDER BY ri.created_at DESC LIMIT ${len(params)}",
            *params,
        )
    return [serialize_item(r) for r in rows]


@router.get("/items/{item_id}")
async def get_item(item_id: UUID) -> dict:
    """One revision plus everything the player needs: sibling revisions and the task's notes."""
    async with acquire() as conn:
        row = await _fetch_item(conn, item_id)
        siblings = await conn.fetch(_ITEM_SELECT + " WHERE ri.task_id = $1 ORDER BY ri.revision DESC", row["task_id"])
        notes = await load_threads(conn, row["task_id"])
    out = serialize_item(row)
    out["revisions"] = [serialize_item(s) for s in siblings]
    out["notes"] = notes
    approved = next((s for s in siblings if s["review_status"] == "approved" and s["id"] != row["id"]), None)
    out["last_approved_id"] = str(approved["id"]) if approved else None
    return out


@router.delete("/items/{item_id}", status_code=204, response_class=Response)
async def delete_item(item_id: UUID, user: CurrentUser = Depends(require_current_user)):
    async with acquire() as conn:
        row = await _fetch_item(conn, item_id)
        if not _is_management(user) and row["submitted_by"] != user.db_id:
            raise HTTPException(status_code=403, detail="Only the submitter or a supervisor can delete this")
        await conn.execute("DELETE FROM review_items WHERE id = $1", item_id)
    return Response(status_code=204)


@router.post("/items/{item_id}/reprocess", status_code=202)
async def reprocess_item(item_id: UUID, user: CurrentUser = Depends(require_current_user)) -> dict:
    async with acquire() as conn:
        await _fetch_item(conn, item_id)
        await conn.execute("UPDATE review_items SET processing_status = 'queued' WHERE id = $1", item_id)
    processing.enqueue(item_id)
    return {"status": "queued"}


# --- decisions -------------------------------------------------------------------------


@router.post("/items/{item_id}/decision")
async def decide(
    item_id: UUID,
    body: dict = Body(...),
    user: CurrentUser = Depends(require_management()),
) -> dict:
    decision = str(body.get("decision") or "").strip().lower()
    if decision not in ("approve", "changes_requested"):
        raise HTTPException(status_code=400, detail="decision must be 'approve' or 'changes_requested'")
    comment = _clean_body(body.get("comment"), required=False)
    async with acquire() as conn:
        row = await _fetch_item(conn, item_id)
        task_id = row["task_id"]
        async with conn.transaction():
            await conn.execute(
                """
                UPDATE review_items SET review_status = $2, decided_by = $3, decided_at = NOW()
                WHERE id = $1
                """,
                item_id,
                "approved" if decision == "approve" else "changes_requested",
                user.db_id,
            )
            if decision == "approve":
                await conn.execute("UPDATE tasks SET status = 'done', updated_at = NOW() WHERE id = $1", task_id)
                verified = await conn.fetch(
                    """
                    UPDATE review_notes SET status = 'verified', updated_at = NOW()
                    WHERE task_id = $1 AND parent_id IS NULL AND status = 'fixed'
                    RETURNING id, visibility
                    """,
                    task_id,
                )
                for n in verified:
                    await _log_status(conn, row, n["id"], n["visibility"], "fixed", "verified", user, item_id,
                                      "Verified on approval")
            else:
                await conn.execute("UPDATE tasks SET status = 'in_progress', updated_at = NOW() WHERE id = $1", task_id)
            if row["version_id"]:
                await conn.execute(
                    "UPDATE versions SET status = $2 WHERE id = $1",
                    row["version_id"],
                    "approved" if decision == "approve" else "rejected",
                )
            await conn.execute(
                """
                INSERT INTO review_notes (project_id, task_id, item_id, kind, body, author_id, meta)
                VALUES ($1, $2, $3, 'decision', $4, $5, $6::jsonb)
                """,
                row["project_id"],
                task_id,
                item_id,
                comment,
                user.db_id,
                json.dumps({"decision": decision}),
            )
            open_notes = await conn.fetchval(
                "SELECT count(*) FROM review_notes WHERE task_id = $1 AND parent_id IS NULL AND status = 'open'",
                task_id,
            )
            payload = {
                "item_id": str(item_id),
                "task_id": str(task_id),
                "task_title": row["task_title"],
                "revision": f"v{row['revision']:03d}",
                "decision": decision,
                "open_notes": open_notes,
            }
            await emit_event(conn, project_id=row["project_id"], actor_id=user.db_id, kind="review.decided", payload=payload)
            people = await task_people(conn, task_id)
            if row["submitted_by"] and row["submitted_by"] not in people:
                people.append(row["submitted_by"])
            if people:
                await emit_notification(
                    conn, project_id=row["project_id"], kind="review.decided", payload=payload, audience=people
                )
        row = await _fetch_item(conn, item_id)
    return serialize_item(row)


# --- notes -----------------------------------------------------------------------------


async def _log_status(conn, item_row, note_id, visibility, old, new, user, item_id, body="") -> None:
    await conn.execute(
        """
        INSERT INTO review_notes (project_id, task_id, item_id, parent_id, kind, body, author_id,
                                  visibility, meta)
        VALUES ($1, $2, $3, $4, 'status', $5, $6, $7, $8::jsonb)
        """,
        item_row["project_id"],
        item_row["task_id"],
        item_id,
        note_id,
        body,
        getattr(user, "db_id", None),
        visibility,
        json.dumps({"from": old, "to": new}),
    )


async def insert_note(
    conn,
    item_row,
    *,
    body: str,
    frame: Optional[int],
    frame_end: Optional[int],
    drawing: Optional[str],
    parent_id: Optional[UUID],
    actionable: bool,
    author_id: Optional[UUID],
    guest_name: Optional[str] = None,
    share_id: Optional[UUID] = None,
    visibility: str = "internal",
):
    if parent_id:
        parent = await conn.fetchrow(
            "SELECT id, task_id, parent_id, visibility FROM review_notes WHERE id = $1", parent_id
        )
        if not parent or parent["task_id"] != item_row["task_id"]:
            raise HTTPException(status_code=404, detail="Note not found")
        if parent["parent_id"] is not None:
            parent_id = parent["parent_id"]  # replies stay one level deep
        visibility = "client" if parent["visibility"] == "client" else visibility
        frame = frame_end = None
        status = None
    else:
        status = "open" if actionable else None
        if frame is not None and frame_end is not None and frame_end < frame:
            frame, frame_end = frame_end, frame
    return await conn.fetchrow(
        """
        INSERT INTO review_notes (project_id, task_id, item_id, parent_id, kind, body, frame, frame_end,
                                  drawing, status, visibility, author_id, guest_name, share_id)
        VALUES ($1, $2, $3, $4, 'comment', $5, $6, $7, $8::jsonb, $9, $10, $11, $12, $13)
        RETURNING id
        """,
        item_row["project_id"],
        item_row["task_id"],
        item_row["id"],
        parent_id,
        body,
        frame,
        frame_end,
        drawing,
        status,
        visibility,
        author_id,
        guest_name,
        share_id,
    )


@router.post("/items/{item_id}/notes", status_code=201)
async def add_note(
    item_id: UUID,
    body: dict = Body(...),
    user: CurrentUser = Depends(require_current_user),
) -> dict:
    """A note on a frame (or frame range), optionally with a drawing; or a reply (parent_id)."""
    drawing = _validate_drawing(body.get("drawing"))
    text = _clean_body(body.get("body"), required=drawing is None)
    parent_id = UUID(str(body["parent_id"])) if body.get("parent_id") else None
    async with acquire() as conn:
        row = await _fetch_item(conn, item_id)
        async with conn.transaction():
            note = await insert_note(
                conn,
                row,
                body=text,
                frame=_opt_int(body.get("frame")),
                frame_end=_opt_int(body.get("frame_end")),
                drawing=drawing,
                parent_id=parent_id,
                actionable=bool(body.get("actionable", True)),
                author_id=user.db_id,
            )
            payload = {
                "item_id": str(item_id),
                "task_id": str(row["task_id"]),
                "task_title": row["task_title"],
                "note_id": str(note["id"]),
                "frame": body.get("frame"),
                "excerpt": text[:140],
            }
            people = [p for p in await task_people(conn, row["task_id"]) if p != user.db_id]
            if people:
                await emit_notification(conn, project_id=row["project_id"], kind="review.note", payload=payload, audience=people)
        threads = await load_threads(conn, row["task_id"])
    return {"id": str(note["id"]), "notes": threads}


def _opt_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="frame must be a whole number") from None


@router.get("/notes")
async def list_notes(
    task_id: UUID = Query(...),
    status: Optional[str] = Query(None, description="open | fixed | verified | unresolved"),
) -> list[dict]:
    """Task notes for DCC panels; ``unresolved`` = open or fixed."""
    async with acquire() as conn:
        await ensure_in_scope(conn, "task", task_id)
        threads = await load_threads(conn, task_id)
    threads = [t for t in threads if t["status"]]
    if status == "unresolved":
        threads = [t for t in threads if t["status"] in ("open", "fixed")]
    elif status:
        threads = [t for t in threads if t["status"] == status]
    return threads


@router.patch("/notes/{note_id}")
async def update_note(
    note_id: UUID,
    body: dict = Body(...),
    user: CurrentUser = Depends(require_current_user),
) -> dict:
    async with acquire() as conn:
        await ensure_in_scope(conn, "review_note", note_id)
        note = await conn.fetchrow("SELECT * FROM review_notes WHERE id = $1", note_id)
        if not note:
            raise HTTPException(status_code=404, detail="Note not found")
        item_row = await _fetch_item(conn, note["item_id"])
        async with conn.transaction():
            if "body" in body:
                if note["author_id"] != user.db_id or note["kind"] != "comment":
                    raise HTTPException(status_code=403, detail="Only the author can edit a note")
                await conn.execute(
                    "UPDATE review_notes SET body = $2, updated_at = NOW() WHERE id = $1",
                    note_id,
                    _clean_body(body["body"], required=note["drawing"] is None),
                )
            if "status" in body:
                new = str(body["status"] or "").lower()
                if new not in NOTE_STATUSES or note["parent_id"] is not None or note["status"] is None:
                    raise HTTPException(status_code=400, detail="Only top-level notes have a status (open, fixed, verified)")
                old = note["status"]
                if new in ("verified", "open") and old != new:
                    if not (_is_management(user) or note["author_id"] == user.db_id):
                        raise HTTPException(status_code=403, detail="Only the reviewer can verify or reopen a note")
                if old != new:
                    fixed_in = None
                    if new == "fixed":
                        fixed_in = UUID(str(body["fixed_in_item_id"])) if body.get("fixed_in_item_id") else None
                        if fixed_in is None:
                            fixed_in = await conn.fetchval(
                                "SELECT id FROM review_items WHERE task_id = $1 ORDER BY revision DESC LIMIT 1",
                                note["task_id"],
                            )
                    await conn.execute(
                        """
                        UPDATE review_notes SET status = $2, updated_at = NOW(),
                               fixed_in_item_id = CASE WHEN $2 = 'open' THEN NULL ELSE COALESCE($3, fixed_in_item_id) END
                        WHERE id = $1
                        """,
                        note_id,
                        new,
                        fixed_in,
                    )
                    await _log_status(
                        conn, item_row, note_id, note["visibility"], old, new, user,
                        fixed_in or note["item_id"], _clean_body(body.get("comment"), required=False),
                    )
                    if new == "fixed" and note["author_id"] and note["author_id"] != user.db_id:
                        await emit_notification(
                            conn,
                            project_id=note["project_id"],
                            kind="review.note_fixed",
                            payload={"task_id": str(note["task_id"]), "note_id": str(note_id),
                                     "item_id": str(fixed_in or note["item_id"]), "excerpt": note["body"][:140]},
                            audience=[note["author_id"]],
                        )
        threads = await load_threads(conn, note["task_id"])
    return {"id": str(note_id), "notes": threads}


@router.delete("/notes/{note_id}")
async def delete_note(note_id: UUID, user: CurrentUser = Depends(require_current_user)) -> dict:
    async with acquire() as conn:
        await ensure_in_scope(conn, "review_note", note_id)
        note = await conn.fetchrow("SELECT author_id, task_id, kind FROM review_notes WHERE id = $1", note_id)
        if not note:
            raise HTTPException(status_code=404, detail="Note not found")
        if note["author_id"] != user.db_id and not _is_management(user):
            raise HTTPException(status_code=403, detail="Only the author or a supervisor can delete a note")
        await conn.execute("DELETE FROM review_notes WHERE id = $1", note_id)
        threads = await load_threads(conn, note["task_id"])
    return {"notes": threads}
