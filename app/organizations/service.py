"""Organization + account creation shared by signup and invite acceptance."""
from __future__ import annotations

import re
from typing import Any, Optional
from uuid import UUID

import asyncpg
from fastapi import HTTPException

from app.auth import service as auth_service

MIN_PASSWORD_LENGTH = 8
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,38}$")


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return slug[:48] or "studio"


def validate_email(email: str) -> str:
    email = (email or "").strip().lower()
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="A valid email is required")
    return email


def validate_password(password: str) -> str:
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=400, detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters"
        )
    return password


async def unique_slug(conn: asyncpg.Connection, base: str) -> str:
    slug = slugify(base)
    candidate, n = slug, 2
    while await conn.fetchval("SELECT 1 FROM organizations WHERE slug = $1", candidate):
        candidate = f"{slug}-{n}"
        n += 1
    return candidate


async def unique_username(conn: asyncpg.Connection, requested: Optional[str], email: str) -> str:
    """Use ``requested`` if given (must be free), else derive one from the email."""
    if requested:
        username = requested.strip().lower()
        if not _USERNAME_RE.match(username):
            raise HTTPException(
                status_code=400,
                detail="Username must be 2-39 characters: letters, numbers, '.', '_' or '-'",
            )
        if await conn.fetchval("SELECT 1 FROM users WHERE username = $1", username):
            raise HTTPException(status_code=409, detail="Username is already taken")
        return username
    base = re.sub(r"[^a-z0-9._-]+", "", email.split("@", 1)[0].lower())[:32] or "user"
    candidate, n = base, 2
    while await conn.fetchval("SELECT 1 FROM users WHERE username = $1", candidate):
        candidate = f"{base}{n}"
        n += 1
    return candidate


async def create_user(
    conn: asyncpg.Connection,
    *,
    organization_id: UUID,
    email: str,
    password: str,
    name: Optional[str],
    app_role: str,
    username: Optional[str] = None,
    department: Optional[str] = None,
) -> asyncpg.Record:
    email = validate_email(email)
    validate_password(password)
    if await conn.fetchval("SELECT 1 FROM users WHERE lower(email) = $1", email):
        raise HTTPException(status_code=409, detail="An account with this email already exists")
    uname = await unique_username(conn, username, email)
    return await conn.fetchrow(
        """
        INSERT INTO users (email, username, password_hash, name, app_role, department,
                           organization_id, is_active)
        VALUES ($1, $2, $3, $4, $5, $6, $7, TRUE)
        RETURNING id, email, username, name, role, app_role, department, organization_id
        """,
        email,
        uname,
        auth_service.hash_password(password),
        (name or "").strip() or uname,
        app_role,
        department,
        organization_id,
    )


async def create_organization_with_owner(
    conn: asyncpg.Connection,
    *,
    studio_name: str,
    email: str,
    password: str,
    name: Optional[str],
    username: Optional[str] = None,
) -> tuple[asyncpg.Record, asyncpg.Record]:
    """Create an organization and its first user (pipeline role = org admin)."""
    studio_name = (studio_name or "").strip()
    if not studio_name:
        raise HTTPException(status_code=400, detail="studio_name is required")
    org = await conn.fetchrow(
        "INSERT INTO organizations (name, slug) VALUES ($1, $2) RETURNING id, name, slug, plan",
        studio_name,
        await unique_slug(conn, studio_name),
    )
    user = await create_user(
        conn,
        organization_id=org["id"],
        email=email,
        password=password,
        name=name,
        app_role="pipeline",
        username=username,
    )
    await conn.execute("UPDATE organizations SET owner_id = $1 WHERE id = $2", user["id"], org["id"])
    return org, user


def organization_payload(row: Any) -> Optional[dict]:
    if not row or not row.get("org_id"):
        return None
    return {"id": str(row["org_id"]), "name": row["org_name"], "slug": row["org_slug"]}
