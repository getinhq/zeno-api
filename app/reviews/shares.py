"""Client review links: let someone outside the studio watch, comment and approve.

Team routes (``/api/v1/reviews/shares``) create, list and revoke links. Public routes
(``/api/v1/review-shares/public/{token}``) need no account: the link token is the
credential (only its SHA-256 is stored), plus an optional password sent as the
``X-Share-Password`` header. Clients only ever see notes made through share links and the
team's replies to them; internal notes stay internal. A client's approve/request-changes is
recorded and announced to the team but does not change task status: the supervisor decides.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Response

import app.config as app_config
from app.auth import service as auth_service
from app.auth.deps import CurrentUser, require_management
from app.db import acquire
from app.events.log import emit as emit_event
from app.notifications.service import emit as emit_notification
from app.reviews.router import (
    _ITEM_SELECT,
    _clean_body,
    _opt_int,
    _validate_drawing,
    insert_note,
    load_threads,
    serialize_item,
)
from app.tenancy import ensure_all_in_scope, ensure_in_scope

router = APIRouter(prefix="/api/v1/reviews/shares", tags=["reviews"])
public_router = APIRouter(prefix="/api/v1/review-shares/public", tags=["reviews"])

SHARE_URL_TTL = 12 * 3600


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _share_out(row, *, link: Optional[str] = None) -> dict:
    now = datetime.now(timezone.utc)
    state = "active"
    if row["revoked_at"]:
        state = "revoked"
    elif row["expires_at"] and row["expires_at"] < now:
        state = "expired"
    out = {
        "id": str(row["id"]),
        "project_id": str(row["project_id"]),
        "title": row["title"],
        "item_ids": [str(i) for i in row["item_ids"]],
        "has_password": bool(row["password_hash"]),
        "allow_decisions": row["allow_decisions"],
        "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
        "last_viewed_at": row["last_viewed_at"].isoformat() if row["last_viewed_at"] else None,
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "state": state,
    }
    if link:
        out["link"] = link
    return out


@router.post("", status_code=201)
async def create_share(body: dict = Body(...), user: CurrentUser = Depends(require_management())) -> dict:
    try:
        item_ids = [UUID(str(i)) for i in body.get("item_ids") or []]
    except ValueError:
        raise HTTPException(status_code=400, detail="item_ids must be review item ids") from None
    if not item_ids or len(item_ids) > 100:
        raise HTTPException(status_code=400, detail="Share between 1 and 100 review items")
    days = body.get("expires_in_days", 14)
    if days is not None and (not isinstance(days, int) or not 1 <= days <= 365):
        raise HTTPException(status_code=400, detail="expires_in_days must be 1-365 or null")
    password = (body.get("password") or "").strip()
    if password and len(password) < 4:
        raise HTTPException(status_code=400, detail="Use a password of at least 4 characters")
    async with acquire() as conn:
        await ensure_all_in_scope(conn, "review_item", item_ids)
        projects = await conn.fetch("SELECT DISTINCT project_id FROM review_items WHERE id = ANY($1::uuid[])", item_ids)
        if len(projects) != 1:
            raise HTTPException(status_code=400, detail="Share items from one project at a time")
        found = await conn.fetchval("SELECT count(*) FROM review_items WHERE id = ANY($1::uuid[])", item_ids)
        if found != len(set(item_ids)):
            raise HTTPException(status_code=404, detail="Review item not found")
        token = secrets.token_urlsafe(32)
        row = await conn.fetchrow(
            """
            INSERT INTO review_shares (project_id, token_hash, title, item_ids, password_hash,
                                       allow_decisions, expires_at, created_by)
            VALUES ($1, $2, $3, $4::uuid[], $5, $6, $7, $8)
            RETURNING *
            """,
            projects[0]["project_id"],
            _hash(token),
            (body.get("title") or "").strip()[:200],
            list(dict.fromkeys(item_ids)),
            auth_service.hash_password(password) if password else None,
            bool(body.get("allow_decisions", True)),
            datetime.now(timezone.utc) + timedelta(days=days) if days else None,
            user.db_id,
        )
        await emit_event(conn, project_id=row["project_id"], actor_id=user.db_id, kind="review.shared",
                         payload={"share_id": str(row["id"]), "items": len(item_ids)})
    return _share_out(row, link=f"{app_config.PUBLIC_APP_URL}/r/{token}")


@router.get("")
async def list_shares(project_id: UUID = Query(...), user: CurrentUser = Depends(require_management())) -> list[dict]:
    async with acquire() as conn:
        await ensure_in_scope(conn, "project", project_id)
        rows = await conn.fetch(
            "SELECT * FROM review_shares WHERE project_id = $1 ORDER BY created_at DESC", project_id
        )
    return [_share_out(r) for r in rows]


@router.delete("/{share_id}", status_code=204, response_class=Response)
async def revoke_share(share_id: UUID, user: CurrentUser = Depends(require_management())):
    async with acquire() as conn:
        await ensure_in_scope(conn, "review_share", share_id)
        await conn.execute(
            "UPDATE review_shares SET revoked_at = COALESCE(revoked_at, NOW()) WHERE id = $1", share_id
        )
    return Response(status_code=204)


# --- public ----------------------------------------------------------------------------


async def _open_share(conn, token: str, password: Optional[str]):
    row = await conn.fetchrow("SELECT * FROM review_shares WHERE token_hash = $1", _hash(token))
    if not row or row["revoked_at"]:
        raise HTTPException(status_code=404, detail="This review link is not valid")
    if row["expires_at"] and row["expires_at"] < datetime.now(timezone.utc):
        raise HTTPException(status_code=410, detail="This review link has expired")
    if row["password_hash"]:
        if not password:
            raise HTTPException(status_code=401, detail="Password required")
        if not auth_service.verify_password(password, row["password_hash"]):
            raise HTTPException(status_code=401, detail="Wrong password")
    return row


def _public_item(row) -> dict:
    item = serialize_item(row)
    # Clients see the media and the shot, not internal people or processing detail.
    for key in ("submitted_by", "decided_by", "source_info", "sequence", "version_id", "open_notes", "fixed_notes"):
        item.pop(key, None)
    return item


@public_router.get("/{token}")
async def open_share(token: str, x_share_password: Optional[str] = Header(None)) -> dict:
    async with acquire() as conn:
        share = await _open_share(conn, token, x_share_password)
        rows = await conn.fetch(
            _ITEM_SELECT + " WHERE ri.id = ANY($1::uuid[]) AND ri.processing_status = 'ready'",
            share["item_ids"],
        )
        order = {iid: i for i, iid in enumerate(share["item_ids"])}
        rows = sorted(rows, key=lambda r: order.get(r["id"], 0))
        items = []
        for r in rows:
            item = _public_item(r)
            threads = await load_threads(conn, r["task_id"], client_only=True)
            item["notes"] = [t for t in threads if t["item_id"] == item["id"] or t["kind"] != "decision"]
            items.append(item)
        project = await conn.fetchrow(
            """
            SELECT p.name, o.name AS studio FROM projects p
            LEFT JOIN organizations o ON o.id = p.organization_id WHERE p.id = $1
            """,
            share["project_id"],
        )
        await conn.execute("UPDATE review_shares SET last_viewed_at = NOW() WHERE id = $1", share["id"])
    return {
        "title": share["title"] or (project["name"] if project else "Review"),
        "project": project["name"] if project else None,
        "studio": project["studio"] if project else None,
        "allow_decisions": share["allow_decisions"],
        "expires_at": share["expires_at"].isoformat() if share["expires_at"] else None,
        "items": items,
    }


async def _share_item(conn, share, item_id: UUID):
    if item_id not in share["item_ids"]:
        raise HTTPException(status_code=404, detail="Not part of this review")
    row = await conn.fetchrow(_ITEM_SELECT + " WHERE ri.id = $1", item_id)
    if not row:
        raise HTTPException(status_code=404, detail="Not part of this review")
    return row


def _guest(body: dict) -> str:
    name = str(body.get("guest_name") or "").strip()
    if not 1 <= len(name) <= 80:
        raise HTTPException(status_code=400, detail="Tell the team your name (up to 80 characters)")
    return name


@public_router.post("/{token}/items/{item_id}/notes", status_code=201)
async def guest_note(
    token: str, item_id: UUID, body: dict = Body(...), x_share_password: Optional[str] = Header(None)
) -> dict:
    guest = _guest(body)
    drawing = _validate_drawing(body.get("drawing"))
    text = _clean_body(body.get("body"), required=drawing is None)
    async with acquire() as conn:
        share = await _open_share(conn, token, x_share_password)
        row = await _share_item(conn, share, item_id)
        parent_id = UUID(str(body["parent_id"])) if body.get("parent_id") else None
        if parent_id:
            vis = await conn.fetchval("SELECT visibility FROM review_notes WHERE id = $1", parent_id)
            if vis != "client":
                raise HTTPException(status_code=404, detail="Note not found")
        async with conn.transaction():
            note = await insert_note(
                conn, row, body=text, frame=_opt_int(body.get("frame")), frame_end=_opt_int(body.get("frame_end")),
                drawing=drawing, parent_id=parent_id, actionable=True, author_id=None,
                guest_name=guest, share_id=share["id"], visibility="client",
            )
            await emit_notification(
                conn, project_id=row["project_id"], kind="review.client_note",
                payload={"item_id": str(item_id), "task_id": str(row["task_id"]), "task_title": row["task_title"],
                         "note_id": str(note["id"]), "guest": guest, "excerpt": text[:140]},
            )
        threads = await load_threads(conn, row["task_id"], client_only=True)
    return {"id": str(note["id"]), "notes": threads}


@public_router.post("/{token}/items/{item_id}/decision")
async def guest_decision(
    token: str, item_id: UUID, body: dict = Body(...), x_share_password: Optional[str] = Header(None)
) -> dict:
    guest = _guest(body)
    decision = str(body.get("decision") or "").lower()
    if decision not in ("approve", "changes_requested"):
        raise HTTPException(status_code=400, detail="decision must be 'approve' or 'changes_requested'")
    comment = _clean_body(body.get("comment"), required=False)
    async with acquire() as conn:
        share = await _open_share(conn, token, x_share_password)
        if not share["allow_decisions"]:
            raise HTTPException(status_code=403, detail="This link is for comments only")
        row = await _share_item(conn, share, item_id)
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO review_notes (project_id, task_id, item_id, kind, body, guest_name, share_id,
                                          visibility, meta)
                VALUES ($1, $2, $3, 'decision', $4, $5, $6, 'client', $7::jsonb)
                """,
                row["project_id"], row["task_id"], item_id, comment, guest, share["id"],
                json.dumps({"decision": decision, "client": True}),
            )
            payload = {"item_id": str(item_id), "task_id": str(row["task_id"]), "task_title": row["task_title"],
                       "revision": f"v{row['revision']:03d}", "decision": decision, "guest": guest}
            await emit_event(conn, project_id=row["project_id"], actor_id=None, kind="review.client_decision", payload=payload)
            await emit_notification(conn, project_id=row["project_id"], kind="review.client_decision", payload=payload)
        threads = await load_threads(conn, row["task_id"], client_only=True)
    return {"decision": decision, "notes": threads}
