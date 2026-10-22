"""Task template storage and task generation."""
from __future__ import annotations

import json
from typing import Any, Optional
from uuid import UUID

import asyncpg
from fastapi import HTTPException

from app.task_templates.defaults import ASSET_TYPES, DEFAULT_TEMPLATES, TASK_TYPES

TEMPLATE_COLUMNS = "id, organization_id, name, entity_kind, asset_type, is_default, steps, created_at, updated_at"


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def serialize(row: Any) -> dict:
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "entity_kind": row["entity_kind"],
        "asset_type": row["asset_type"],
        "is_default": bool(row["is_default"]),
        "steps": _json(row["steps"]) or [],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


def validate_definition(body: dict, *, partial: bool = False) -> dict:
    """Validate a template create/update body; returns the normalized fields present."""
    out: dict[str, Any] = {}
    if "name" in body or not partial:
        name = (body.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="name is required")
        out["name"] = name
    if "entity_kind" in body or not partial:
        kind = (body.get("entity_kind") or "asset").strip().lower()
        if kind not in ("asset", "shot"):
            raise HTTPException(status_code=400, detail="entity_kind must be 'asset' or 'shot'")
        out["entity_kind"] = kind
    if "asset_type" in body or not partial:
        asset_type = (body.get("asset_type") or "").strip().lower() or None
        if asset_type and asset_type not in ASSET_TYPES:
            raise HTTPException(status_code=400, detail=f"asset_type must be one of {', '.join(ASSET_TYPES)}")
        out["asset_type"] = asset_type
    if out.get("entity_kind") == "shot":
        out["asset_type"] = None
    if "steps" in body or not partial:
        out["steps"] = validate_steps(body.get("steps") or [])
    if "is_default" in body:
        out["is_default"] = bool(body["is_default"])
    return out


def validate_steps(steps: Any) -> list[dict]:
    if not isinstance(steps, list) or not steps:
        raise HTTPException(status_code=400, detail="steps must be a non-empty list")
    if len(steps) > 50:
        raise HTTPException(status_code=400, detail="A template can have at most 50 steps")
    out = []
    for i, raw in enumerate(steps):
        if not isinstance(raw, dict):
            raise HTTPException(status_code=400, detail=f"step {i + 1} must be an object")
        t = str(raw.get("type") or "").strip().lower()
        if t not in TASK_TYPES:
            raise HTTPException(status_code=400, detail=f"step {i + 1}: type must be one of {', '.join(TASK_TYPES)}")
        title = str(raw.get("title") or t.capitalize()).strip()[:120]
        try:
            days = int(raw.get("duration_days", 1))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"step {i + 1}: duration_days must be a number") from None
        if not 0 <= days <= 365:
            raise HTTPException(status_code=400, detail=f"step {i + 1}: duration_days must be 0-365")
        deps = raw.get("depends_on") or []
        if not isinstance(deps, list) or any(not isinstance(d, int) or d < 0 or d >= i for d in deps):
            raise HTTPException(
                status_code=400, detail=f"step {i + 1}: depends_on may only reference earlier steps"
            )
        out.append({"type": t, "title": title, "duration_days": days, "depends_on": sorted(set(deps))})
    return out


async def ensure_defaults(conn: asyncpg.Connection, org_id: UUID) -> None:
    """Give an organization the built-in templates the first time it needs templates."""
    if org_id is None or await conn.fetchval(
        "SELECT 1 FROM task_templates WHERE organization_id = $1 LIMIT 1", org_id
    ):
        return
    for t in DEFAULT_TEMPLATES:
        await conn.execute(
            """
            INSERT INTO task_templates (organization_id, name, entity_kind, asset_type, is_default, steps)
            VALUES ($1, $2, $3, $4, TRUE, $5::jsonb)
            ON CONFLICT DO NOTHING
            """,
            org_id,
            t["name"],
            t["entity_kind"],
            t["asset_type"],
            json.dumps(t["steps"]),
        )


async def default_template(
    conn: asyncpg.Connection, org_id: UUID, entity_kind: str, asset_type: Optional[str]
) -> Optional[asyncpg.Record]:
    await ensure_defaults(conn, org_id)
    return await conn.fetchrow(
        f"""
        SELECT {TEMPLATE_COLUMNS} FROM task_templates
        WHERE organization_id = $1 AND entity_kind = $2
          AND COALESCE(asset_type, '') = COALESCE($3, '') AND is_default
        """,
        org_id,
        entity_kind,
        asset_type if entity_kind == "asset" else None,
    )


async def apply_template(
    conn: asyncpg.Connection,
    template: Any,
    *,
    project_id: UUID,
    asset: Optional[Any] = None,
    shot: Optional[Any] = None,
) -> dict[int, UUID]:
    """
    Create one task per template step for an asset or shot. Steps whose task type the
    entity already has are skipped, so applying twice is harmless. Returns
    ``{step_index: task_id}`` for every step (existing or new) so callers can link them.
    """
    steps = _json(template["steps"]) or []
    if asset is not None:
        entity_col, entity_id = "asset_id", asset["id"]
        label = asset["name"] or asset["code"]
    else:
        entity_col, entity_id = "shot_id", shot["id"]
        label = shot["shot_code"]
    existing = {
        r["type"]: r["id"]
        for r in await conn.fetch(
            f"SELECT type, id FROM tasks WHERE {entity_col} = $1 ORDER BY created_at", entity_id
        )
    }
    created: dict[int, UUID] = {}
    for i, step in enumerate(steps):
        if step["type"] in existing:
            created[i] = existing[step["type"]]
            continue
        task_id = await conn.fetchval(
            f"""
            INSERT INTO tasks (project_id, {entity_col}, type, title, status, estimated_hours, metadata)
            VALUES ($1, $2, $3, $4, 'todo', $5, $6::jsonb)
            RETURNING id
            """,
            project_id,
            entity_id,
            step["type"],
            f"{step['title']} · {label}",
            step.get("duration_days", 0) * 8 or None,
            json.dumps(
                {
                    "template_id": str(template["id"]),
                    "template_step": i,
                    "duration_days": step.get("duration_days", 0),
                }
            ),
        )
        existing[step["type"]] = task_id
        created[i] = task_id
    return created


async def apply_default_for_asset(conn: asyncpg.Connection, asset: Any) -> int:
    """Create the default tasks for a new asset. Returns how many tasks were created."""
    org_id = await conn.fetchval("SELECT organization_id FROM projects WHERE id = $1", asset["project_id"])
    template = await default_template(conn, org_id, "asset", asset["type"])
    if template is None:
        return 0
    before = await conn.fetchval("SELECT count(*) FROM tasks WHERE asset_id = $1", asset["id"])
    await apply_template(conn, template, project_id=asset["project_id"], asset=asset)
    return await conn.fetchval("SELECT count(*) FROM tasks WHERE asset_id = $1", asset["id"]) - before


async def apply_default_for_shot(conn: asyncpg.Connection, shot: Any) -> int:
    """Create the default tasks for a new shot. Returns how many tasks were created."""
    row = await conn.fetchrow(
        """
        SELECT e.project_id, p.organization_id
        FROM sequences sq
        JOIN episodes e ON e.id = sq.episode_id
        JOIN projects p ON p.id = e.project_id
        WHERE sq.id = $1
        """,
        shot["sequence_id"],
    )
    if row is None:
        return 0
    template = await default_template(conn, row["organization_id"], "shot", None)
    if template is None:
        return 0
    before = await conn.fetchval("SELECT count(*) FROM tasks WHERE shot_id = $1", shot["id"])
    await apply_template(conn, template, project_id=row["project_id"], shot=shot)
    return await conn.fetchval("SELECT count(*) FROM tasks WHERE shot_id = $1", shot["id"]) - before
