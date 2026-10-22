"""Current organization: read it, rename it."""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException

from app import mailer
from app.auth.deps import CurrentUser, require_current_user, require_role
from app.db import acquire
from app.tenancy import current_org_id, default_org_id_for_unscoped

router = APIRouter(prefix="/api/v1/organization", tags=["organization"])


async def _load(conn) -> dict:
    org_id = current_org_id() or await default_org_id_for_unscoped(conn)
    row = await conn.fetchrow(
        """
        SELECT o.id, o.name, o.slug, o.plan, o.owner_id, o.created_at,
               (SELECT count(*) FROM users u WHERE u.organization_id = o.id AND u.is_active) AS members,
               (SELECT count(*) FROM projects p WHERE p.organization_id = o.id) AS projects
        FROM organizations o WHERE o.id = $1
        """,
        org_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Organization not found")
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "slug": row["slug"],
        "plan": row["plan"],
        "owner_id": str(row["owner_id"]) if row["owner_id"] else None,
        "members": int(row["members"]),
        "projects": int(row["projects"]),
        "email_configured": mailer.is_configured(),
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


@router.get("")
async def get_organization(_user: CurrentUser = Depends(require_current_user)) -> dict:
    async with acquire() as conn:
        return await _load(conn)


@router.patch("")
async def update_organization(
    body: dict = Body(...),
    _user: CurrentUser = Depends(require_role("pipeline")),
) -> dict:
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    async with acquire() as conn:
        org_id = current_org_id() or await default_org_id_for_unscoped(conn)
        await conn.execute("UPDATE organizations SET name = $1 WHERE id = $2", name, org_id)
        return await _load(conn)


@router.delete("")
async def delete_organization(
    body: dict = Body(...),
    user: CurrentUser = Depends(require_current_user),
) -> dict:
    """
    Permanently delete the caller's studio: its people, projects, tasks, versions, invites
    and settings rows. Owner only, and ``confirm_name`` must match the studio name exactly.
    Published file blobs are content-addressed and may be shared, so they are not removed here.
    """
    async with acquire() as conn:
        org_id = current_org_id()
        if org_id is None:
            raise HTTPException(status_code=400, detail="Sign in to delete a studio")
        org = await conn.fetchrow("SELECT id, name, owner_id FROM organizations WHERE id = $1", org_id)
        if org is None:
            raise HTTPException(status_code=404, detail="Organization not found")
        if org["owner_id"] != user.db_id:
            raise HTTPException(status_code=403, detail="Only the studio owner can delete the studio")
        if (body.get("confirm_name") or "") != org["name"]:
            raise HTTPException(status_code=400, detail="Type the studio name exactly to confirm")
        async with conn.transaction():
            await conn.execute("DELETE FROM organizations WHERE id = $1", org_id)
    return {"deleted": str(org_id)}

