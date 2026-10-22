"""Project templates — create a whole project skeleton (structure, milestones, starter assets)."""
from __future__ import annotations

import json
import re
from datetime import date, timedelta
from typing import Any

import asyncpg
from fastapi import APIRouter, Body, Depends, HTTPException

from app.auth.deps import MANAGEMENT_ROLES, CurrentUser, require_current_user, require_role
from app.db import acquire
from app.events import log as events_log
from app.project_templates.definitions import (
    LIMITS,
    MAX_STARTER_ASSETS,
    MAX_TOTAL_SHOTS,
    PROJECT_TEMPLATES,
    TEMPLATES_BY_ID,
)
from app.task_templates.defaults import ASSET_TYPES
from app.task_templates.service import apply_default_for_asset, apply_default_for_shot
from app.tenancy import current_org_id, default_org_id_for_unscoped

router = APIRouter(prefix="/api/v1", tags=["project-templates"])


@router.get("/project-templates")
async def list_project_templates(_user: CurrentUser = Depends(require_current_user)) -> list[dict]:
    return PROJECT_TEMPLATES


def _parse_date(value: Any, field: str) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{field} must be YYYY-MM-DD") from None


def _structure(template: dict, overrides: Any) -> dict[str, int]:
    out = dict(template["structure"])
    if isinstance(overrides, dict):
        for key, limit in LIMITS.items():
            if key in overrides:
                try:
                    value = int(overrides[key])
                except (TypeError, ValueError):
                    raise HTTPException(status_code=400, detail=f"{key} must be a number") from None
                if not 0 <= value <= limit:
                    raise HTTPException(status_code=400, detail=f"{key} must be between 0 and {limit}")
                out[key] = value
    total = out["episodes"] * out["sequences_per_episode"] * out["shots_per_sequence"]
    if total > MAX_TOTAL_SHOTS:
        raise HTTPException(
            status_code=400, detail=f"That would create {total} shots; the limit is {MAX_TOTAL_SHOTS}"
        )
    if out["frames_per_shot"] < 1:
        out["frames_per_shot"] = 1
    return out


def _starter_assets(raw: Any) -> list[tuple[str, str]]:
    """{"character": ["Nono", "Grandma"], ...} -> [(type, name), ...] (deduplicated)."""
    if not raw:
        return []
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="assets must be an object keyed by asset type")
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for a_type, names in raw.items():
        if a_type not in ASSET_TYPES:
            raise HTTPException(status_code=400, detail=f"Unknown asset type: {a_type}")
        if isinstance(names, str):
            names = re.split(r"[\n,]", names)
        for name in names or []:
            name = str(name).strip()
            if name and (a_type, name.lower()) not in seen:
                seen.add((a_type, name.lower()))
                out.append((a_type, name[:120]))
    if len(out) > MAX_STARTER_ASSETS:
        raise HTTPException(status_code=400, detail=f"At most {MAX_STARTER_ASSETS} starter assets")
    return out


def _asset_code(name: str, used: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:40] or "asset"
    code, n = base, 2
    while code in used:
        code = f"{base}_{n}"
        n += 1
    used.add(code)
    return code


@router.post("/projects/from-template", status_code=201)
async def create_project_from_template(
    body: dict = Body(...),
    user: CurrentUser = Depends(require_role(*MANAGEMENT_ROLES)),
) -> dict:
    """
    Body: template, name, code, start_date?, end_date?, structure? (episodes,
    sequences_per_episode, shots_per_sequence, frames_per_shot), assets?
    ({"character": ["Nono"], "prop": "Mug, Lamp"}), fps?, resolution?
    """
    template = TEMPLATES_BY_ID.get(str(body.get("template") or ""))
    if template is None:
        raise HTTPException(status_code=400, detail="Unknown project template")
    name = (body.get("name") or "").strip()
    code = (body.get("code") or "").strip()
    if not name or not code:
        raise HTTPException(status_code=400, detail="name and code are required")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", code):
        raise HTTPException(
            status_code=400, detail="code may use letters, numbers, '-' and '_' (max 32 characters)"
        )

    start = _parse_date(body.get("start_date"), "start_date") or date.today()
    end = _parse_date(body.get("end_date"), "end_date") or start + timedelta(days=template["default_duration_days"])
    if end <= start:
        raise HTTPException(status_code=400, detail="end_date must be after start_date")
    structure = _structure(template, body.get("structure"))
    starter_assets = _starter_assets(body.get("assets"))
    metadata = {
        "template": template["id"],
        "fps": int(body.get("fps") or template["fps"]),
        "resolution": str(body.get("resolution") or template["resolution"]),
    }

    counts = {"episodes": 0, "sequences": 0, "shots": 0, "assets": 0, "tasks": 0, "milestones": 0}
    async with acquire() as conn:
        org_id = current_org_id() or await default_org_id_for_unscoped(conn)
        async with conn.transaction():
            try:
                project = await conn.fetchrow(
                    """
                    INSERT INTO projects (name, code, status, start_date, end_date, metadata, organization_id)
                    VALUES ($1, $2, 'active', $3, $4, $5::jsonb, $6)
                    RETURNING id, name, code, status, start_date, end_date, created_at
                    """,
                    name,
                    code,
                    start,
                    end,
                    json.dumps(metadata),
                    org_id,
                )
            except asyncpg.UniqueViolationError:
                raise HTTPException(
                    status_code=409, detail="A project with this name or code already exists"
                ) from None
            pid = project["id"]

            span = (end - start).days
            for m in template["milestones"]:
                await conn.execute(
                    "INSERT INTO milestones (project_id, name, due_date, kind) VALUES ($1, $2, $3, $4)",
                    pid,
                    m["name"],
                    start + timedelta(days=round(span * m["at"])),
                    m["kind"],
                )
                counts["milestones"] += 1

            seq_names = template.get("sequence_names") or []
            frames = structure["frames_per_shot"]
            for e in range(1, structure["episodes"] + 1):
                ep_label = template["episode_label"]
                episode = await conn.fetchrow(
                    """
                    INSERT INTO episodes (project_id, episode_number, title, code)
                    VALUES ($1, $2, $3, $4) RETURNING id
                    """,
                    pid,
                    e,
                    f"{ep_label} {e}" if structure["episodes"] > 1 else ep_label,
                    f"EP{e:02d}",
                )
                counts["episodes"] += 1
                for s in range(1, structure["sequences_per_episode"] + 1):
                    seq_code = f"SQ{s * 10:03d}"
                    seq_name = seq_names[s - 1] if s - 1 < len(seq_names) else f"Sequence {s * 10:03d}"
                    sequence = await conn.fetchrow(
                        """
                        INSERT INTO sequences (episode_id, name, code, stage)
                        VALUES ($1, $2, $3, 'Animatics') RETURNING id
                        """,
                        episode["id"],
                        seq_name,
                        seq_code,
                    )
                    counts["sequences"] += 1
                    for sh in range(1, structure["shots_per_sequence"] + 1):
                        shot = await conn.fetchrow(
                            """
                            INSERT INTO shots (sequence_id, shot_code, stage, frame_start, frame_end)
                            VALUES ($1, $2, 'Animatics', 1001, $3)
                            RETURNING id, sequence_id, shot_code
                            """,
                            sequence["id"],
                            f"SH{sh * 10:04d}",
                            1001 + frames - 1,
                        )
                        counts["shots"] += 1
                        counts["tasks"] += await apply_default_for_shot(conn, shot)

            used_codes: set[str] = set()
            for a_type, a_name in starter_assets:
                asset = await conn.fetchrow(
                    """
                    INSERT INTO assets (project_id, type, name, code)
                    VALUES ($1, $2, $3, $4)
                    RETURNING id, project_id, type, name, code
                    """,
                    pid,
                    a_type,
                    a_name,
                    _asset_code(a_name, used_codes),
                )
                counts["assets"] += 1
                counts["tasks"] += await apply_default_for_asset(conn, asset)

            await events_log.emit(
                conn,
                project_id=pid,
                actor_id=user.db_id,
                kind="project.created",
                payload={"template": template["id"], **counts},
            )

    return {
        "project": {
            "id": str(project["id"]),
            "name": project["name"],
            "code": project["code"],
            "status": project["status"],
            "start_date": project["start_date"].isoformat(),
            "end_date": project["end_date"].isoformat(),
            "metadata": metadata,
        },
        "created": counts,
    }
