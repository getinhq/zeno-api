"""Scene facts: PUT/GET /api/v1/shots/{id}/scene-facts, POST /api/v1/versions/lookup.

A DCC reports what the shot file contains (on Submit for Review, for example). Zeno keeps the
latest report on the shot (``shots.metadata.scene_facts``) and answers with a comparison:
linked asset versions against the newest approved ones, and the file's assets against the
shot's casting.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException

from app.auth.deps import CurrentUser, require_current_user
from app.db import acquire
from app.scene_facts.report import build_report, clean_facts
from app.tenancy import current_org_id, ensure_in_scope

router = APIRouter(prefix="/api/v1", tags=["scene-facts"])

_VERSION_SELECT = """
    SELECT v.id AS version_id, v.content_id, v.asset_id, v.representation, v.pipeline_stage,
           v.version_number, v.status, a.code AS asset_code, a.name AS asset_name, a.project_id
    FROM versions v
    JOIN assets a ON a.id = v.asset_id
    JOIN projects p ON p.id = a.project_id
    WHERE v.content_id = ANY($1::text[]) AND ($2::uuid IS NULL OR p.organization_id = $2)
"""


async def _versions_by_cid(conn, cids: list[str], project_id: UUID | None = None) -> dict[str, dict[str, Any]]:
    if not cids:
        return {}
    rows = await conn.fetch(_VERSION_SELECT + " ORDER BY v.version_number DESC", cids, current_org_id())
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        if project_id is not None and r["project_id"] != project_id:
            continue
        out.setdefault(r["content_id"], dict(r))  # same bytes published twice: report the newest
    return out


async def _report(conn, shot: Any, facts: dict[str, Any]) -> dict[str, Any]:
    cids = sorted({a["content_id"] for a in facts.get("assets") or [] if a.get("content_id")})
    versions = await _versions_by_cid(conn, cids, shot["project_id"])
    asset_ids = sorted({v["asset_id"] for v in versions.values()})
    approved: dict[tuple[str, str, str], int] = {}
    if asset_ids:
        for r in await conn.fetch(
            """
            SELECT asset_id, representation, pipeline_stage, MAX(version_number) AS n
            FROM versions WHERE asset_id = ANY($1::uuid[]) AND status = 'approved'
            GROUP BY asset_id, representation, pipeline_stage
            """,
            asset_ids,
        ):
            approved[(str(r["asset_id"]), str(r["representation"]), str(r["pipeline_stage"] or ""))] = int(r["n"])
    casting = [
        dict(r)
        for r in await conn.fetch(
            """
            SELECT sa.asset_id, a.code, a.name FROM shot_assets sa JOIN assets a ON a.id = sa.asset_id
            WHERE sa.shot_id = $1 AND sa.is_active
            """,
            shot["id"],
        )
    ]
    report = build_report(facts, versions_by_cid=versions, approved_latest=approved, casting=casting)
    rng = (shot["frame_start"], shot["frame_end"])
    report["shot"] = {"code": shot["shot_code"], "frame_start": rng[0], "frame_end": rng[1],
                      "handle_in": shot["handle_in"], "handle_out": shot["handle_out"]}
    report["range_matches"] = None if None in rng else (facts.get("frame_start"), facts.get("frame_end")) == rng
    return report


async def _shot(conn, shot_id: UUID):
    await ensure_in_scope(conn, "shot", shot_id)
    row = await conn.fetchrow(
        """
        SELECT sh.id, sh.shot_code, sh.frame_start, sh.frame_end, sh.handle_in, sh.handle_out, sh.metadata,
               e.project_id
        FROM shots sh JOIN sequences s ON s.id = sh.sequence_id JOIN episodes e ON e.id = s.episode_id
        WHERE sh.id = $1
        """,
        shot_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Shot not found")
    return row


def _meta(value: Any) -> dict:
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


@router.put("/shots/{shot_id}/scene-facts")
async def put_scene_facts(
    shot_id: UUID,
    body: dict = Body(...),
    user: CurrentUser = Depends(require_current_user),
) -> dict:
    """Store what the shot file contains; returns the stored facts and the comparison."""
    facts = clean_facts(body)
    facts["reported_at"] = datetime.now(timezone.utc).isoformat()
    facts["reported_by"] = str(user.db_id) if user.db_id else None
    async with acquire() as conn:
        shot = await _shot(conn, shot_id)
        meta = _meta(shot["metadata"])
        meta["scene_facts"] = facts
        await conn.execute("UPDATE shots SET metadata = $2::jsonb, updated_at = NOW() WHERE id = $1", shot_id, json.dumps(meta))
        report = await _report(conn, shot, facts)
    return {"facts": facts, "report": report}


@router.get("/shots/{shot_id}/scene-facts")
async def get_scene_facts(shot_id: UUID, _user: CurrentUser = Depends(require_current_user)) -> dict:
    """The last report for the shot (``facts`` is null before any DCC reported)."""
    async with acquire() as conn:
        shot = await _shot(conn, shot_id)
        facts = _meta(shot["metadata"]).get("scene_facts")
        report = await _report(conn, shot, facts) if facts else None
    return {"facts": facts, "report": report}


@router.post("/versions/lookup")
async def lookup_versions(body: dict = Body(...), _user: CurrentUser = Depends(require_current_user)) -> list[dict]:
    """Which asset versions have these content ids (``{"content_ids": [...]}``, at most 500)."""
    cids = [str(c).strip().lower() for c in (body.get("content_ids") or [])][:500]
    async with acquire() as conn:
        found = await _versions_by_cid(conn, cids)
    return [
        {
            "content_id": cid,
            "version_id": str(v["version_id"]),
            "asset_id": str(v["asset_id"]),
            "asset_code": v["asset_code"],
            "asset_name": v["asset_name"],
            "representation": v["representation"],
            "pipeline_stage": v["pipeline_stage"] or "",
            "version": int(v["version_number"]),
            "status": v["status"],
        }
        for cid, v in found.items()
    ]
