"""Invites — studio admins invite people by link (and by email when SMTP is configured).

Management routes are org-scoped. The two ``/public/{token}`` routes are reachable without
a session: the invite token itself is the credential, and only its SHA-256 is stored.
"""
from __future__ import annotations

import hashlib
import html
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException

import app.config as app_config
from app import mailer
from app.auth.deps import MANAGEMENT_ROLES, CurrentUser, require_role
from app.db import acquire
from app.organizations.service import create_user, validate_email
from app.tenancy import current_org_id, default_org_id_for_unscoped, ensure_in_scope

router = APIRouter(prefix="/api/v1/invites", tags=["invites"])

APP_ROLES = ("artist", "pipeline", "supervisor", "production")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _invite_link(token: str) -> str:
    return f"{app_config.PUBLIC_APP_URL}/invite/{token}"


def _status(row: Any) -> str:
    if row["revoked_at"]:
        return "revoked"
    if row["accepted_at"]:
        return "accepted"
    if row["expires_at"] < datetime.now(timezone.utc):
        return "expired"
    return "pending"


def _serialize(row: Any) -> dict:
    return {
        "id": str(row["id"]),
        "email": row["email"],
        "app_role": row["app_role"],
        "department": row["department"],
        "project_id": str(row["project_id"]) if row["project_id"] else None,
        "message": row["message"],
        "invited_by": str(row["invited_by"]) if row["invited_by"] else None,
        "invited_by_name": row.get("invited_by_name"),
        "status": _status(row),
        "expires_at": row["expires_at"].isoformat(),
        "accepted_at": row["accepted_at"].isoformat() if row["accepted_at"] else None,
        "email_sent_at": row["email_sent_at"].isoformat() if row["email_sent_at"] else None,
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


_SELECT = """
    SELECT i.*, COALESCE(u.name, u.username) AS invited_by_name
    FROM invites i
    LEFT JOIN users u ON u.id = i.invited_by
"""


async def _send_invite_email(row: Any, token: str, org_name: str, inviter: Optional[str]) -> bool:
    if not row["email"] or not mailer.is_configured():
        return False
    link = _invite_link(token)
    who = inviter or "Your studio"
    note = f"\n\n“{row['message']}”" if row["message"] else ""
    text = (
        f"{who} invited you to join {org_name} on Zeno as {row['app_role']}.{note}\n\n"
        f"Accept the invite: {link}\n\nThis link expires on {row['expires_at']:%d %b %Y}."
    )
    note_html = f"<blockquote>{html.escape(row['message'])}</blockquote>" if row["message"] else ""
    body_html = (
        f"<p>{html.escape(who)} invited you to join <b>{html.escape(org_name)}</b> on Zeno "
        f"as <b>{html.escape(row['app_role'])}</b>.</p>{note_html}"
        f'<p><a href="{html.escape(link)}">Accept the invite</a></p>'
        f"<p style='color:#777'>This link expires on {row['expires_at']:%d %b %Y}.</p>"
    )
    sent = await mailer.send(row["email"], f"Join {org_name} on Zeno", text, body_html)
    if sent:
        async with acquire() as conn:
            await conn.execute("UPDATE invites SET email_sent_at = NOW() WHERE id = $1", row["id"])
    return sent


@router.post("", status_code=201)
async def create_invite(
    body: dict = Body(...),
    user: CurrentUser = Depends(require_role(*MANAGEMENT_ROLES)),
) -> dict:
    """
    Create an invite. Body: app_role (default artist), email?, department?, project_id?,
    message?. Returns the invite plus its one-time-visible ``link``; the link is the only
    copy of the token, so it cannot be shown again later (use /regenerate).
    """
    app_role = (body.get("app_role") or "artist").strip().lower()
    if app_role not in APP_ROLES:
        raise HTTPException(status_code=400, detail=f"app_role must be one of {', '.join(APP_ROLES)}")
    email = validate_email(body["email"]) if body.get("email") else None
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=app_config.INVITE_TTL_DAYS)

    async with acquire() as conn:
        org_id = current_org_id() or await default_org_id_for_unscoped(conn)
        await ensure_in_scope(conn, "project", body.get("project_id"))
        if email and await conn.fetchval("SELECT 1 FROM users WHERE lower(email) = $1", email):
            raise HTTPException(status_code=409, detail="Someone with this email already has an account")
        row = await conn.fetchrow(
            """
            INSERT INTO invites (organization_id, email, app_role, department, project_id,
                                 message, token_hash, invited_by, expires_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            RETURNING *
            """,
            org_id,
            email,
            app_role,
            (body.get("department") or "").strip() or None,
            body.get("project_id") or None,
            (body.get("message") or "").strip() or None,
            _hash(token),
            user.db_id,
            expires,
        )
        org_name = await conn.fetchval("SELECT name FROM organizations WHERE id = $1", org_id)
    email_sent = await _send_invite_email(row, token, org_name, user.username if user.db_id else None)
    out = _serialize(row)
    out.update(link=_invite_link(token), email_sent=email_sent, email_configured=mailer.is_configured())
    return out


@router.get("")
async def list_invites(
    _user: CurrentUser = Depends(require_role(*MANAGEMENT_ROLES)),
) -> list[dict]:
    async with acquire() as conn:
        org_id = current_org_id() or await default_org_id_for_unscoped(conn)
        rows = await conn.fetch(
            _SELECT + " WHERE i.organization_id = $1 ORDER BY i.created_at DESC", org_id
        )
    return [_serialize(r) for r in rows]


@router.post("/{invite_id}/regenerate")
async def regenerate_invite(
    invite_id: UUID,
    user: CurrentUser = Depends(require_role(*MANAGEMENT_ROLES)),
) -> dict:
    """Issue a fresh link (old one stops working) and extend the expiry; re-sends the email."""
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=app_config.INVITE_TTL_DAYS)
    async with acquire() as conn:
        await ensure_in_scope(conn, "invite", invite_id)
        row = await conn.fetchrow(
            """
            UPDATE invites SET token_hash = $1, expires_at = $2, revoked_at = NULL
            WHERE id = $3 AND accepted_at IS NULL
            RETURNING *
            """,
            _hash(token),
            expires,
            invite_id,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Invite not found or already accepted")
        org_name = await conn.fetchval(
            "SELECT name FROM organizations WHERE id = $1", row["organization_id"]
        )
    email_sent = await _send_invite_email(row, token, org_name, user.username if user.db_id else None)
    out = _serialize(row)
    out.update(link=_invite_link(token), email_sent=email_sent, email_configured=mailer.is_configured())
    return out


@router.delete("/{invite_id}")
async def revoke_invite(
    invite_id: UUID,
    _user: CurrentUser = Depends(require_role(*MANAGEMENT_ROLES)),
) -> dict:
    async with acquire() as conn:
        await ensure_in_scope(conn, "invite", invite_id)
        row = await conn.fetchrow(
            "UPDATE invites SET revoked_at = NOW() WHERE id = $1 AND accepted_at IS NULL RETURNING *",
            invite_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Invite not found or already accepted")
    return _serialize(row)


# --- Public (token is the credential) --------------------------------------------------


async def _load_by_token(conn, token: str) -> Any:
    row = await conn.fetchrow(
        _SELECT.replace("SELECT i.*", "SELECT i.*, o.name AS org_name")
        + " JOIN organizations o ON o.id = i.organization_id WHERE i.token_hash = $1",
        _hash(token),
    )
    if not row:
        raise HTTPException(status_code=404, detail="This invite link is not valid")
    status = _status(row)
    if status != "pending":
        raise HTTPException(status_code=410, detail=f"This invite has been {status}")
    return row


@router.get("/public/{token}")
async def preview_invite(token: str) -> dict:
    """What the accept page shows before the person signs up."""
    async with acquire() as conn:
        row = await _load_by_token(conn, token)
    return {
        "organization": row["org_name"],
        "email": row["email"],
        "app_role": row["app_role"],
        "invited_by": row["invited_by_name"],
        "message": row["message"],
        "expires_at": row["expires_at"].isoformat(),
    }


@router.post("/public/{token}/accept", status_code=201)
async def accept_invite(token: str, body: dict = Body(...)) -> dict:
    """Create the account in the inviting org and sign the person in."""
    from app.auth.router import _USER_SELECT, _token_response

    async with acquire() as conn:
        async with conn.transaction():
            row = await _load_by_token(conn, token)
            # Lock the invite so two tabs cannot accept it twice.
            await conn.execute("SELECT 1 FROM invites WHERE id = $1 FOR UPDATE", row["id"])
            if await conn.fetchval("SELECT accepted_at FROM invites WHERE id = $1", row["id"]):
                raise HTTPException(status_code=410, detail="This invite has been accepted")
            email = row["email"] or body.get("email") or ""
            user = await create_user(
                conn,
                organization_id=row["organization_id"],
                email=email,
                password=body.get("password") or "",
                name=body.get("name"),
                app_role=row["app_role"],
                username=body.get("username"),
                department=row["department"],
            )
            await conn.execute(
                "UPDATE invites SET accepted_at = NOW(), accepted_user_id = $1 WHERE id = $2",
                user["id"],
                row["id"],
            )
            if row["project_id"]:
                await conn.execute(
                    "INSERT INTO project_members (project_id, user_id) VALUES ($1, $2)"
                    " ON CONFLICT DO NOTHING",
                    row["project_id"],
                    user["id"],
                )
        session_row = await conn.fetchrow(_USER_SELECT + " WHERE u.id = $1", user["id"])
    return _token_response(session_row)
