"""Task templates API — per-studio templates that generate tasks for assets and shots."""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Body, Depends, HTTPException

from app.auth.deps import MANAGEMENT_ROLES, CurrentUser, require_current_user, require_role
from app.db import acquire
from app.task_templates import service
from app.task_templates.defaults import ASSET_TYPES, TASK_TYPES
from app.tenancy import current_org_id, default_org_id_for_unscoped, ensure_all_in_scope, ensure_in_scope

router = APIRouter(prefix="/api/v1/task-templates", tags=["task-templates"])


async def _org(conn) -> UUID:
    org_id = current_org_id() or await default_org_id_for_unscoped(conn)
    if org_id is None:
        raise HTTPException(status_code=400, detail="No organization")
    return org_id


async def _clear_other_defaults(conn, org_id: UUID, fields: dict, keep_id: Any = None) -> None:
    await conn.execute(
        """
        UPDATE task_templates SET is_default = FALSE
        WHERE organization_id = $1 AND entity_kind = $2
          AND COALESCE(asset_type, '') = COALESCE($3, '') AND is_default
          AND ($4::uuid IS NULL OR id <> $4)
        """,
        org_id,
        fields["entity_kind"],
        fields["asset_type"],
        keep_id,
    )


@router.get("/vocabulary")
async def vocabulary(_user: CurrentUser = Depends(require_current_user)) -> dict:
    """Task and asset types the template editor can choose from."""
    return {"task_types": list(TASK_TYPES), "asset_types": list(ASSET_TYPES)}


@router.get("")
async def list_templates(_user: CurrentUser = Depends(require_current_user)) -> list[dict]:
    async with acquire() as conn:
        org_id = await _org(conn)
        await service.ensure_defaults(conn, org_id)
        rows = await conn.fetch(
            f"SELECT {service.TEMPLATE_COLUMNS} FROM task_templates WHERE organization_id = $1"
            " ORDER BY entity_kind, asset_type NULLS LAST, is_default DESC, name",
            org_id,
        )
    return [service.serialize(r) for r in rows]


@router.post("", status_code=201)
async def create_template(
    body: dict = Body(...), _user: CurrentUser = Depends(require_role(*MANAGEMENT_ROLES))
) -> dict:
    fields = service.validate_definition(body)
    async with acquire() as conn:
        org_id = await _org(conn)
        await service.ensure_defaults(conn, org_id)
        async with conn.transaction():
            if fields.get("is_default"):
                await _clear_other_defaults(conn, org_id, fields)
            row = await conn.fetchrow(
                f"""
                INSERT INTO task_templates (organization_id, name, entity_kind, asset_type, is_default, steps)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb)
                RETURNING {service.TEMPLATE_COLUMNS}
                """,
                org_id,
                fields["name"],
                fields["entity_kind"],
                fields["asset_type"],
                fields.get("is_default", False),
                json.dumps(fields["steps"]),
            )
    return service.serialize(row)


@router.patch("/{template_id}")
async def update_template(
    template_id: UUID,
    body: dict = Body(...),
    _user: CurrentUser = Depends(require_role(*MANAGEMENT_ROLES)),
) -> dict:
    fields = service.validate_definition(body, partial=True)
    if not fields:
        raise HTTPException(status_code=400, detail="No updatable fields provided")
    async with acquire() as conn:
        await ensure_in_scope(conn, "task_template", template_id)
        current = await conn.fetchrow(
            f"SELECT {service.TEMPLATE_COLUMNS} FROM task_templates WHERE id = $1", template_id
        )
        if current is None:
            raise HTTPException(status_code=404, detail="Task template not found")
        merged = {
            "entity_kind": fields.get("entity_kind", current["entity_kind"]),
            "asset_type": fields.get("asset_type", current["asset_type"]),
        }
        if merged["entity_kind"] == "shot":
            merged["asset_type"] = None
        sets, params = [], []
        for key in ("name", "entity_kind", "is_default"):
            if key in fields:
                params.append(fields[key])
                sets.append(f"{key} = ${len(params)}")
        if "entity_kind" in fields or "asset_type" in fields:
            params.append(merged["asset_type"])
            sets.append(f"asset_type = ${len(params)}")
        if "steps" in fields:
            params.append(json.dumps(fields["steps"]))
            sets.append(f"steps = ${len(params)}::jsonb")
        params.append(template_id)
        async with conn.transaction():
            if fields.get("is_default", current["is_default"]):
                await _clear_other_defaults(conn, current["organization_id"], merged, keep_id=template_id)
            row = await conn.fetchrow(
                f"UPDATE task_templates SET {', '.join(sets)} WHERE id = ${len(params)}"
                f" RETURNING {service.TEMPLATE_COLUMNS}",
                *params,
            )
    return service.serialize(row)


@router.delete("/{template_id}")
async def delete_template(
    template_id: UUID, _user: CurrentUser = Depends(require_role(*MANAGEMENT_ROLES))
) -> dict:
    async with acquire() as conn:
        await ensure_in_scope(conn, "task_template", template_id)
        deleted = await conn.fetchval("DELETE FROM task_templates WHERE id = $1 RETURNING id", template_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Task template not found")
    return {"deleted": str(deleted)}


@router.post("/{template_id}/apply")
async def apply_template(
    template_id: UUID,
    body: dict = Body(...),
    _user: CurrentUser = Depends(require_role(*MANAGEMENT_ROLES)),
) -> dict:
    """Create the template's tasks on existing assets or shots (skips task types they already have)."""
    asset_ids = list(body.get("asset_ids") or [])
    shot_ids = list(body.get("shot_ids") or [])
    if not asset_ids and not shot_ids:
        raise HTTPException(status_code=400, detail="Provide asset_ids or shot_ids")
    async with acquire() as conn:
        await ensure_in_scope(conn, "task_template", template_id)
        template = await conn.fetchrow(
            f"SELECT {service.TEMPLATE_COLUMNS} FROM task_templates WHERE id = $1", template_id
        )
        if template is None:
            raise HTTPException(status_code=404, detail="Task template not found")
        expected = "asset" if asset_ids else "shot"
        if template["entity_kind"] != expected:
            raise HTTPException(
                status_code=400, detail=f"This template is for {template['entity_kind']}s"
            )
        await ensure_all_in_scope(conn, "asset", asset_ids)
        await ensure_all_in_scope(conn, "shot", shot_ids)
        created = 0
        async with conn.transaction():
            for aid in asset_ids:
                asset = await conn.fetchrow(
                    "SELECT id, project_id, name, code, type FROM assets WHERE id = $1", UUID(str(aid))
                )
                if asset is None:
                    continue
                before = await conn.fetchval("SELECT count(*) FROM tasks WHERE asset_id = $1", asset["id"])
                await service.apply_template(conn, template, project_id=asset["project_id"], asset=asset)
                created += await conn.fetchval("SELECT count(*) FROM tasks WHERE asset_id = $1", asset["id"]) - before
            for sid in shot_ids:
                shot = await conn.fetchrow(
                    """
                    SELECT s.id, s.sequence_id, s.shot_code, e.project_id
                    FROM shots s JOIN sequences sq ON sq.id = s.sequence_id
                    JOIN episodes e ON e.id = sq.episode_id WHERE s.id = $1
                    """,
                    UUID(str(sid)),
                )
                if shot is None:
                    continue
                before = await conn.fetchval("SELECT count(*) FROM tasks WHERE shot_id = $1", shot["id"])
                await service.apply_template(conn, template, project_id=shot["project_id"], shot=shot)
                created += await conn.fetchval("SELECT count(*) FROM tasks WHERE shot_id = $1", shot["id"]) - before
    return {"tasks_created": created}
