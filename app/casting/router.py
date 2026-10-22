"""Shot casting (breakdown): which assets appear in which shots."""
from __future__ import annotations

from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException

from app.auth.deps import MANAGEMENT_ROLES, CurrentUser, require_current_user, require_role
from app.db import acquire
from app.tenancy import ensure_all_in_scope, ensure_in_scope

router = APIRouter(prefix="/api/v1", tags=["casting"])

_SHOT_PROJECT_SQL = """
    SELECT e.project_id FROM shots s
    JOIN sequences sq ON sq.id = s.sequence_id
    JOIN episodes e ON e.id = sq.episode_id
    WHERE s.id = $1
"""

_CAST_SELECT = """
    SELECT sa.id, sa.shot_id, sa.asset_id, sa.role, sa.instance_name, sa.sort_order,
           a.code AS asset_code, a.name AS asset_name, a.type AS asset_type,
           s.shot_code, sq.code AS sequence_code, e.code AS episode_code
    FROM shot_assets sa
    JOIN assets a     ON a.id = sa.asset_id
    JOIN shots s      ON s.id = sa.shot_id
    JOIN sequences sq ON sq.id = s.sequence_id
    JOIN episodes e   ON e.id = sq.episode_id
"""


def _serialize(row: Any) -> dict:
    return {
        "id": str(row["id"]),
        "shot_id": str(row["shot_id"]),
        "asset_id": str(row["asset_id"]),
        "role": row["role"],
        "instance_name": row["instance_name"] or None,
        "sort_order": row["sort_order"],
        "asset": {"code": row["asset_code"], "name": row["asset_name"], "type": row["asset_type"]},
        "shot": {
            "code": row["shot_code"],
            "sequence_code": row["sequence_code"],
            "episode_code": row["episode_code"],
        },
    }


async def _assert_same_project(conn, shot_id, asset_ids: list) -> UUID:
    project_id = await conn.fetchval(_SHOT_PROJECT_SQL, shot_id)
    if project_id is None:
        raise HTTPException(status_code=404, detail="Shot not found")
    if asset_ids:
        bad = await conn.fetchval(
            "SELECT count(*) FROM assets WHERE id = ANY($1::uuid[]) AND project_id <> $2",
            [str(a) for a in asset_ids],
            project_id,
        )
        if bad:
            raise HTTPException(status_code=400, detail="Assets must belong to the shot's project")
    return project_id


async def _insert(conn, shot_id, asset_id, role: Optional[str], instance: str, actor) -> None:
    await conn.execute(
        """
        INSERT INTO shot_assets (shot_id, asset_id, role, instance_name, created_by, sort_order)
        VALUES ($1, $2, COALESCE($3, (SELECT type FROM assets WHERE id = $2)), $4, $5,
                (SELECT COALESCE(max(sort_order), 0) + 1 FROM shot_assets WHERE shot_id = $1))
        ON CONFLICT (shot_id, asset_id, instance_name) DO NOTHING
        """,
        shot_id,
        asset_id,
        role,
        instance,
        actor,
    )


@router.get("/shots/{shot_id}/casting")
async def get_shot_casting(shot_id: UUID, _user: CurrentUser = Depends(require_current_user)) -> list[dict]:
    async with acquire() as conn:
        await ensure_in_scope(conn, "shot", shot_id)
        rows = await conn.fetch(_CAST_SELECT + " WHERE sa.shot_id = $1 ORDER BY sa.sort_order, a.code", shot_id)
    return [_serialize(r) for r in rows]


@router.post("/shots/{shot_id}/casting", status_code=201)
async def add_to_shot(
    shot_id: UUID,
    body: dict = Body(...),
    user: CurrentUser = Depends(require_role(*MANAGEMENT_ROLES)),
) -> list[dict]:
    """Cast one asset. Body: asset_id, role?, instance_name? (e.g. 'crowd_02' for a second copy)."""
    asset_id = body.get("asset_id")
    if not asset_id:
        raise HTTPException(status_code=400, detail="asset_id is required")
    async with acquire() as conn:
        await ensure_in_scope(conn, "shot", shot_id)
        await ensure_in_scope(conn, "asset", asset_id)
        await _assert_same_project(conn, shot_id, [asset_id])
        await _insert(conn, shot_id, UUID(str(asset_id)), body.get("role"), (body.get("instance_name") or "").strip(), user.db_id)
        rows = await conn.fetch(_CAST_SELECT + " WHERE sa.shot_id = $1 ORDER BY sa.sort_order, a.code", shot_id)
    return [_serialize(r) for r in rows]


@router.put("/shots/{shot_id}/casting")
async def replace_shot_casting(
    shot_id: UUID,
    body: dict = Body(...),
    user: CurrentUser = Depends(require_role(*MANAGEMENT_ROLES)),
) -> list[dict]:
    """Replace the shot's casting. Body: items=[{asset_id, role?, instance_name?}, ...]."""
    items = body.get("items")
    if not isinstance(items, list):
        raise HTTPException(status_code=400, detail="items must be a list")
    asset_ids = [i.get("asset_id") for i in items if isinstance(i, dict)]
    async with acquire() as conn:
        await ensure_in_scope(conn, "shot", shot_id)
        await ensure_all_in_scope(conn, "asset", asset_ids)
        await _assert_same_project(conn, shot_id, asset_ids)
        async with conn.transaction():
            await conn.execute("DELETE FROM shot_assets WHERE shot_id = $1", shot_id)
            for item in items:
                await _insert(
                    conn,
                    shot_id,
                    UUID(str(item["asset_id"])),
                    item.get("role"),
                    (item.get("instance_name") or "").strip(),
                    user.db_id,
                )
        rows = await conn.fetch(_CAST_SELECT + " WHERE sa.shot_id = $1 ORDER BY sa.sort_order, a.code", shot_id)
    return [_serialize(r) for r in rows]


@router.delete("/casting/{casting_id}")
async def remove_casting(
    casting_id: UUID, _user: CurrentUser = Depends(require_role(*MANAGEMENT_ROLES))
) -> dict:
    async with acquire() as conn:
        shot_id = await conn.fetchval("SELECT shot_id FROM shot_assets WHERE id = $1", casting_id)
        if shot_id is None:
            raise HTTPException(status_code=404, detail="Casting not found")
        await ensure_in_scope(conn, "shot", shot_id)
        await conn.execute("DELETE FROM shot_assets WHERE id = $1", casting_id)
    return {"deleted": str(casting_id)}


@router.get("/assets/{asset_id}/shots")
async def shots_for_asset(asset_id: UUID, _user: CurrentUser = Depends(require_current_user)) -> list[dict]:
    """Where an asset appears (its 'appears in' list)."""
    async with acquire() as conn:
        await ensure_in_scope(conn, "asset", asset_id)
        rows = await conn.fetch(
            _CAST_SELECT + " WHERE sa.asset_id = $1 ORDER BY e.code, sq.code, s.shot_code", asset_id
        )
    return [_serialize(r) for r in rows]


@router.get("/projects/{project_id}/breakdown")
async def project_breakdown(project_id: UUID, _user: CurrentUser = Depends(require_current_user)) -> dict:
    """Everything a casting grid needs: shots (in order), assets, and the links between them."""
    async with acquire() as conn:
        await ensure_in_scope(conn, "project", project_id)
        shots = await conn.fetch(
            """
            SELECT s.id, s.shot_code, s.frame_start, s.frame_end, sq.id AS sequence_id, sq.code AS sequence_code,
                   sq.name AS sequence_name, e.code AS episode_code
            FROM shots s
            JOIN sequences sq ON sq.id = s.sequence_id
            JOIN episodes e   ON e.id = sq.episode_id
            WHERE e.project_id = $1
            ORDER BY e.episode_number, e.code, sq.code, s.shot_code
            """,
            project_id,
        )
        assets = await conn.fetch(
            "SELECT id, code, name, type FROM assets WHERE project_id = $1 ORDER BY type, code", project_id
        )
        links = await conn.fetch(
            """
            SELECT sa.id, sa.shot_id, sa.asset_id, sa.role, sa.instance_name
            FROM shot_assets sa JOIN assets a ON a.id = sa.asset_id
            WHERE a.project_id = $1
            """,
            project_id,
        )
    return {
        "shots": [
            {
                "id": str(r["id"]),
                "code": r["shot_code"],
                "frame_start": r["frame_start"],
                "frame_end": r["frame_end"],
                "sequence_id": str(r["sequence_id"]),
                "sequence_code": r["sequence_code"],
                "sequence_name": r["sequence_name"],
                "episode_code": r["episode_code"],
            }
            for r in shots
        ],
        "assets": [{"id": str(r["id"]), "code": r["code"], "name": r["name"], "type": r["type"]} for r in assets],
        "links": [
            {
                "id": str(r["id"]),
                "shot_id": str(r["shot_id"]),
                "asset_id": str(r["asset_id"]),
                "role": r["role"],
                "instance_name": r["instance_name"] or None,
            }
            for r in links
        ],
    }


def _pairs(raw: Any) -> list[dict]:
    """Validate [{shot_id, asset_id, role?}] and normalize ids to UUID strings."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise HTTPException(status_code=400, detail="add/remove must be lists")
    out = []
    for x in raw:
        try:
            out.append({
                "shot_id": str(UUID(str(x["shot_id"]))),
                "asset_id": str(UUID(str(x["asset_id"]))),
                "role": x.get("role"),
            })
        except (KeyError, TypeError, ValueError, AttributeError):
            raise HTTPException(status_code=400, detail="Each change needs a valid shot_id and asset_id") from None
    return out


@router.post("/projects/{project_id}/breakdown")
async def update_breakdown(
    project_id: UUID,
    body: dict = Body(...),
    user: CurrentUser = Depends(require_role(*MANAGEMENT_ROLES)),
) -> dict:
    """Bulk toggle for the grid. Body: add=[{shot_id, asset_id}], remove=[{shot_id, asset_id}]."""
    add = _pairs(body.get("add"))
    remove = _pairs(body.get("remove"))
    if len(add) + len(remove) > 2000:
        raise HTTPException(status_code=400, detail="At most 2000 changes per request")
    shot_ids = {x["shot_id"] for x in add + remove}
    asset_ids = {x["asset_id"] for x in add + remove}
    async with acquire() as conn:
        await ensure_in_scope(conn, "project", project_id)
        bad_shots = await conn.fetchval(
            """
            SELECT count(*) FROM shots s JOIN sequences sq ON sq.id = s.sequence_id
            JOIN episodes e ON e.id = sq.episode_id
            WHERE s.id = ANY($1::uuid[]) AND e.project_id <> $2
            """,
            list(shot_ids),
            project_id,
        )
        bad_assets = await conn.fetchval(
            "SELECT count(*) FROM assets WHERE id = ANY($1::uuid[]) AND project_id <> $2",
            list(asset_ids),
            project_id,
        )
        known = await conn.fetchval(
            """
            SELECT (SELECT count(*) FROM shots s JOIN sequences sq ON sq.id = s.sequence_id
                    JOIN episodes e ON e.id = sq.episode_id
                    WHERE s.id = ANY($1::uuid[]) AND e.project_id = $3)
                 + (SELECT count(*) FROM assets WHERE id = ANY($2::uuid[]) AND project_id = $3)
            """,
            list(shot_ids),
            list(asset_ids),
            project_id,
        )
        if bad_shots or bad_assets or known != len(shot_ids) + len(asset_ids):
            raise HTTPException(status_code=400, detail="Every shot and asset must belong to this project")
        async with conn.transaction():
            for x in remove:
                await conn.execute(
                    "DELETE FROM shot_assets WHERE shot_id = $1 AND asset_id = $2",
                    UUID(str(x["shot_id"])),
                    UUID(str(x["asset_id"])),
                )
            for x in add:
                await _insert(conn, UUID(str(x["shot_id"])), UUID(str(x["asset_id"])), x.get("role"), "", user.db_id)
    return {"added": len(add), "removed": len(remove)}
