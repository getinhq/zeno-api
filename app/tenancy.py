"""Multi-tenant request scoping.

Every request passes through ``bind_request_identity`` (an app-level dependency). It
decodes the bearer token when present, loads the caller's organization, and stores both
in context variables for the rest of the request:

- **Scoped** (a valid token was sent): every read and write is limited to the caller's
  organization. Entities from other organizations answer 404, as if they did not exist.
- **Unscoped** (no token, and ``ENABLE_AUTH`` is off): legacy single-tenant dev mode, used
  by DCC plugins that do not log in yet. Nothing is filtered.

When ``ENABLE_AUTH`` is on, every non-public route requires a valid token, so production
deployments are always scoped.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterable, Optional
from uuid import UUID

from fastapi import HTTPException, Request

import app.config as app_config
from app.auth.deps import CurrentUser, get_current_user

DEFAULT_ORG_SLUG = "default"


@dataclass(frozen=True)
class OrgScope:
    id: UUID
    slug: str
    name: str


_current_org: ContextVar[Optional[OrgScope]] = ContextVar("zeno_current_org", default=None)
_current_user: ContextVar[Optional[CurrentUser]] = ContextVar("zeno_current_user", default=None)

# Reachable without a token even when ENABLE_AUTH is on.
_PUBLIC_EXACT = {
    "/",
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/docs/oauth2-redirect",
    "/api/v1/auth/login",
    "/api/v1/auth/refresh",
    "/api/v1/auth/logout",
    "/api/v1/auth/signup",
    "/api/v1/auth/config",
}
_PUBLIC_PREFIXES = (
    # Invite preview/accept: the invite token itself is the credential.
    "/api/v1/invites/public/",
)


def _is_public(request: Request) -> bool:
    path = request.url.path.rstrip("/") or "/"
    if request.method == "OPTIONS":
        return True
    if path in _PUBLIC_EXACT or path.startswith(_PUBLIC_PREFIXES):
        return True
    # Launch-token exchange: called by the local bridge with the one-time token only.
    if request.method == "GET" and path.startswith("/api/v1/launch-tokens/"):
        return True
    return False


async def bind_request_identity(request: Request) -> None:
    """App-level dependency: authenticate (when required) and bind the org scope."""
    _current_org.set(None)
    _current_user.set(None)
    public = _is_public(request)
    authorization = request.headers.get("authorization")

    if not authorization:
        if app_config.ENABLE_AUTH and not public:
            raise HTTPException(status_code=401, detail="Missing bearer token")
        return

    try:
        user = await get_current_user(authorization=authorization)
    except HTTPException:
        if public or not app_config.ENABLE_AUTH:
            # A stale token on a public or dev route should not lock the caller out.
            return
        raise

    org = await _load_org_for_user(user.id)
    if org is None:
        if public:
            return
        raise HTTPException(status_code=401, detail="User is inactive or has no organization")
    _current_user.set(user)
    _current_org.set(org)


async def _load_org_for_user(user_id: UUID) -> Optional[OrgScope]:
    from app.db import acquire

    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT o.id, o.slug, o.name
            FROM users u
            JOIN organizations o ON o.id = u.organization_id
            WHERE u.id = $1 AND u.is_active
            """,
            user_id,
        )
    if not row:
        return None
    return OrgScope(id=row["id"], slug=row["slug"], name=row["name"])


def current_org() -> Optional[OrgScope]:
    return _current_org.get()


def current_org_id() -> Optional[UUID]:
    org = _current_org.get()
    return org.id if org else None


def current_user() -> Optional[CurrentUser]:
    return _current_user.get()


def settings_key(env: str) -> str:
    """
    Key for the Mongo ``settings_global`` document of the current organization.

    The legacy default organization keeps using the plain ``env`` document so existing
    single-tenant settings carry over; every other organization gets its own document.
    """
    org = _current_org.get()
    if org is None or org.slug == DEFAULT_ORG_SLUG:
        return env
    return f"{env}@{org.id}"


def lock_namespace() -> str:
    """Prefix for Redis lock/presence keys so project codes cannot collide across orgs."""
    org = _current_org.get()
    if org is None or org.slug == DEFAULT_ORG_SLUG:
        return ""
    return f"{org.id}:"


# --- Entity ownership checks ---------------------------------------------------------

_TASK_ORG_SQL = """
    SELECT p.organization_id
    FROM tasks t
    LEFT JOIN shots s      ON s.id = t.shot_id
    LEFT JOIN sequences sq ON sq.id = s.sequence_id
    LEFT JOIN episodes e   ON e.id = sq.episode_id
    LEFT JOIN assets a     ON a.id = t.asset_id
    JOIN projects p        ON p.id = COALESCE(t.project_id, e.project_id, a.project_id)
    WHERE t.id = $1
"""

_ORG_SQL = {
    "project": "SELECT organization_id FROM projects WHERE id = $1",
    "episode": """
        SELECT p.organization_id FROM episodes e
        JOIN projects p ON p.id = e.project_id WHERE e.id = $1
    """,
    "sequence": """
        SELECT p.organization_id FROM sequences sq
        JOIN episodes e ON e.id = sq.episode_id
        JOIN projects p ON p.id = e.project_id WHERE sq.id = $1
    """,
    "shot": """
        SELECT p.organization_id FROM shots s
        JOIN sequences sq ON sq.id = s.sequence_id
        JOIN episodes e   ON e.id = sq.episode_id
        JOIN projects p   ON p.id = e.project_id WHERE s.id = $1
    """,
    "asset": """
        SELECT p.organization_id FROM assets a
        JOIN projects p ON p.id = a.project_id WHERE a.id = $1
    """,
    "version": """
        SELECT p.organization_id FROM versions v
        JOIN assets a   ON a.id = v.asset_id
        JOIN projects p ON p.id = a.project_id WHERE v.id = $1
    """,
    "issue": """
        SELECT p.organization_id FROM issues i
        JOIN projects p ON p.id = i.project_id WHERE i.id = $1
    """,
    "task": _TASK_ORG_SQL,
    "milestone": """
        SELECT p.organization_id FROM milestones m
        JOIN projects p ON p.id = m.project_id WHERE m.id = $1
    """,
    "task_template": "SELECT organization_id FROM task_templates WHERE id = $1",
    "invite": "SELECT organization_id FROM invites WHERE id = $1",
    "user": "SELECT organization_id FROM users WHERE id = $1",
}


async def ensure_in_scope(conn, kind: str, entity_id) -> None:
    """
    Raise 404 when ``entity_id`` is not owned by the caller's organization.
    No-op when the request is unscoped (dev mode) or ``entity_id`` is empty.
    """
    org_id = current_org_id()
    if org_id is None or entity_id in (None, ""):
        return
    try:
        uid = entity_id if isinstance(entity_id, UUID) else UUID(str(entity_id))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"Invalid {kind} id") from None
    owner = await conn.fetchval(_ORG_SQL[kind], uid)
    if owner != org_id:
        raise HTTPException(status_code=404, detail=f"{kind.replace('_', ' ').capitalize()} not found")


async def ensure_all_in_scope(conn, kind: str, ids: Iterable) -> None:
    for entity_id in {str(i) for i in ids if i}:
        await ensure_in_scope(conn, kind, entity_id)


def org_filter_sql(column: str, param_index: int) -> tuple[str, list]:
    """
    ``(sql_fragment, params)`` restricting ``column`` to the caller's organization.
    Returns ``("TRUE", [])`` when unscoped so callers can always AND it in.
    """
    org_id = current_org_id()
    if org_id is None:
        return "TRUE", []
    return f"{column} = ${param_index}", [org_id]


async def default_org_id_for_unscoped(conn) -> Optional[UUID]:
    """Organization assigned to rows created in unscoped dev mode (the oldest org)."""
    return await conn.fetchval("SELECT id FROM organizations ORDER BY created_at LIMIT 1")
