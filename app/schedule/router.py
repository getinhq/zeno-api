"""Schedule API: Gantt data, auto-scheduling, task dependencies, milestones."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException

from app.auth.deps import MANAGEMENT_ROLES, CurrentUser, require_current_user, require_role
from app.db import acquire
from app.schedule import engine
from app.tenancy import ensure_in_scope

router = APIRouter(prefix="/api/v1", tags=["schedule"])

# Which asset task a shot task waits for, when linking from casting.
CASTING_LINKS = {
    "layout": ("model",),
    "animation": ("rig",),
    "cfx": ("groom", "rig"),
    "fx": ("model",),
    "lighting": ("lookdev", "texture"),
}

_PROJECT_TASKS_SQL = """
    SELECT t.id, t.type, t.title, t.status, t.start_date, t.due_date::date AS due_date,
           t.estimated_hours, t.metadata, t.asset_id, t.shot_id, t.assignee_id,
           a.code AS asset_code, a.name AS asset_name, a.type AS asset_type,
           s.shot_code, sq.code AS sequence_code, e.code AS episode_code
    FROM tasks t
    LEFT JOIN assets a      ON a.id = t.asset_id
    LEFT JOIN shots s       ON s.id = t.shot_id
    LEFT JOIN sequences sq  ON sq.id = s.sequence_id
    LEFT JOIN episodes e    ON e.id = sq.episode_id
    WHERE COALESCE(t.project_id, e.project_id, a.project_id) = $1
"""


def _meta(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            v = json.loads(value)
            return v if isinstance(v, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _parse_date(value: Any, field: str) -> Optional[date]:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{field} must be YYYY-MM-DD") from None


async def _project(conn, project_id: UUID) -> Any:
    await ensure_in_scope(conn, "project", project_id)
    row = await conn.fetchrow("SELECT id, code, name, start_date, end_date, created_at FROM projects WHERE id = $1", project_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return row


async def _project_edges(conn, project_id: UUID) -> list[tuple]:
    rows = await conn.fetch(
        """
        SELECT d.task_id, d.depends_on_task_id, d.source
        FROM task_dependencies d
        WHERE d.task_id IN (SELECT id FROM (""" + _PROJECT_TASKS_SQL + """) pt)
        """,
        project_id,
    )
    return rows


def _milestone(row: Any) -> dict:
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "due_date": row["due_date"].isoformat(),
        "kind": row["kind"],
        "description": row["description"],
        "done": row["done_at"] is not None,
    }


@router.get("/projects/{project_id}/schedule")
async def get_schedule(project_id: UUID, _user: CurrentUser = Depends(require_current_user)) -> dict:
    async with acquire() as conn:
        project = await _project(conn, project_id)
        tasks = await conn.fetch(_PROJECT_TASKS_SQL + " ORDER BY t.created_at", project_id)
        edges = await _project_edges(conn, project_id)
        milestones = await conn.fetch("SELECT * FROM milestones WHERE project_id = $1 ORDER BY due_date", project_id)
        assignees = await conn.fetch(
            "SELECT ta.task_id, ta.user_id FROM task_assignees ta WHERE ta.task_id = ANY($1::uuid[])",
            [t["id"] for t in tasks],
        )
    by_task: dict[str, list[str]] = {}
    for r in assignees:
        by_task.setdefault(str(r["task_id"]), []).append(str(r["user_id"]))
    out_tasks = []
    for t in tasks:
        if t["asset_id"]:
            group = {"kind": "asset", "id": str(t["asset_id"]), "label": t["asset_name"] or t["asset_code"], "sub": t["asset_type"]}
        elif t["shot_id"]:
            group = {
                "kind": "shot",
                "id": str(t["shot_id"]),
                "label": f"{t['sequence_code']} / {t['shot_code']}",
                "sub": t["episode_code"],
            }
        else:
            group = {"kind": "project", "id": str(project_id), "label": "General", "sub": None}
        ids = set(by_task.get(str(t["id"]), []))
        if t["assignee_id"]:
            ids.add(str(t["assignee_id"]))
        out_tasks.append({
            "id": str(t["id"]),
            "title": t["title"] or t["type"],
            "type": t["type"],
            "status": t["status"],
            "start_date": t["start_date"].isoformat() if t["start_date"] else None,
            "due_date": t["due_date"].isoformat() if t["due_date"] else None,
            "duration_days": engine.task_duration_days(_meta(t["metadata"]), t["estimated_hours"]),
            "assignees": sorted(ids),
            "group": group,
        })
    return {
        "project": {
            "id": str(project["id"]),
            "code": project["code"],
            "name": project["name"],
            "start_date": project["start_date"].isoformat() if project["start_date"] else None,
            "end_date": project["end_date"].isoformat() if project["end_date"] else None,
        },
        "tasks": out_tasks,
        "dependencies": [
            {"task_id": str(e["task_id"]), "depends_on": str(e["depends_on_task_id"]), "source": e["source"]}
            for e in edges
        ],
        "milestones": [_milestone(m) for m in milestones],
    }


async def _link_from_casting(conn, project_id: UUID) -> int:
    """Create shot-task -> asset-task dependencies for every cast asset (see CASTING_LINKS)."""
    created = 0
    for shot_type, asset_types in CASTING_LINKS.items():
        for asset_type in asset_types:
            result = await conn.execute(
                """
                INSERT INTO task_dependencies (task_id, depends_on_task_id, source)
                SELECT st.id, at.id, 'casting'
                FROM tasks st
                JOIN shot_assets sa ON sa.shot_id = st.shot_id
                JOIN tasks at       ON at.asset_id = sa.asset_id AND at.type = $3
                JOIN assets a       ON a.id = sa.asset_id
                WHERE st.type = $2 AND a.project_id = $1
                  -- only the first available asset task type in the preference list
                  AND NOT EXISTS (
                      SELECT 1 FROM tasks earlier
                      WHERE earlier.asset_id = sa.asset_id
                        AND earlier.type = ANY($4::text[])
                  )
                ON CONFLICT DO NOTHING
                """,
                project_id,
                shot_type,
                asset_type,
                list(asset_types[: asset_types.index(asset_type)]),
            )
            created += int(result.split()[-1])
    return created


@router.post("/projects/{project_id}/schedule/auto")
async def auto_schedule(
    project_id: UUID,
    body: dict = Body(default_factory=dict),
    _user: CurrentUser = Depends(require_role(*MANAGEMENT_ROLES)),
) -> dict:
    """
    Lay out every task on working days in dependency order.
    Body: start_date? (default project start or today), keep_existing? (false),
    link_casting? (true: shot tasks wait for the cast assets' tasks).
    """
    keep_existing = bool(body.get("keep_existing", False))
    async with acquire() as conn:
        project = await _project(conn, project_id)
        start = _parse_date(body.get("start_date"), "start_date") or project["start_date"] or date.today()
        async with conn.transaction():
            links = await _link_from_casting(conn, project_id) if body.get("link_casting", True) else 0
            rows = await conn.fetch(_PROJECT_TASKS_SQL, project_id)
            edges = [(e["task_id"], e["depends_on_task_id"]) for e in await _project_edges(conn, project_id)]
            tasks = [
                {
                    "id": r["id"],
                    "status": r["status"],
                    "start_date": r["start_date"],
                    "due_date": r["due_date"],
                    "duration": engine.task_duration_days(_meta(r["metadata"]), r["estimated_hours"]),
                }
                for r in rows
            ]
            try:
                plan = engine.schedule(tasks, edges, start, keep_existing=keep_existing)
            except engine.CycleError as e:
                raise HTTPException(status_code=409, detail=str(e)) from e
            await conn.executemany(
                "UPDATE tasks SET start_date = $2, due_date = $3::date WHERE id = $1",
                [(tid, s, e) for tid, (s, e) in plan.items()],
            )
    finish = max((e for _, e in plan.values()), default=None)
    deadline = project["end_date"]
    return {
        "scheduled": len(plan),
        "casting_links_created": links,
        "start_date": start.isoformat(),
        "finish_date": finish.isoformat() if finish else None,
        "deadline": deadline.isoformat() if deadline else None,
        "late_by_days": (finish - deadline).days if finish and deadline and finish > deadline else 0,
    }


@router.patch("/tasks/{task_id}/dates")
async def set_task_dates(
    task_id: UUID,
    body: dict = Body(...),
    _user: CurrentUser = Depends(require_role(*MANAGEMENT_ROLES)),
) -> dict:
    """Move/resize a bar. Body: start_date, due_date (YYYY-MM-DD; either may be null to clear)."""
    start = _parse_date(body.get("start_date"), "start_date")
    due = _parse_date(body.get("due_date"), "due_date")
    if start and due and due < start:
        raise HTTPException(status_code=400, detail="due_date cannot be before start_date")
    async with acquire() as conn:
        await ensure_in_scope(conn, "task", task_id)
        row = await conn.fetchrow(
            "UPDATE tasks SET start_date = $2, due_date = $3::date WHERE id = $1"
            " RETURNING id, start_date, due_date::date AS due_date",
            task_id,
            start,
            due,
        )
    if row is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return {
        "id": str(row["id"]),
        "start_date": row["start_date"].isoformat() if row["start_date"] else None,
        "due_date": row["due_date"].isoformat() if row["due_date"] else None,
    }


@router.post("/tasks/{task_id}/dependencies", status_code=201)
async def add_dependency(
    task_id: UUID,
    body: dict = Body(...),
    _user: CurrentUser = Depends(require_role(*MANAGEMENT_ROLES)),
) -> dict:
    """Body: depends_on (task id that must finish first)."""
    try:
        dep = UUID(str(body.get("depends_on")))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="depends_on must be a task id") from None
    if dep == task_id:
        raise HTTPException(status_code=400, detail="A task cannot depend on itself")
    async with acquire() as conn:
        await ensure_in_scope(conn, "task", task_id)
        await ensure_in_scope(conn, "task", dep)
        projects = await conn.fetch(
            """
            SELECT DISTINCT COALESCE(t.project_id, e.project_id, a.project_id) AS pid
            FROM tasks t
            LEFT JOIN shots s ON s.id = t.shot_id
            LEFT JOIN sequences sq ON sq.id = s.sequence_id
            LEFT JOIN episodes e ON e.id = sq.episode_id
            LEFT JOIN assets a ON a.id = t.asset_id
            WHERE t.id = ANY($1::uuid[])
            """,
            [task_id, dep],
        )
        if len(projects) != 1:
            raise HTTPException(status_code=400, detail="Both tasks must be in the same project")
        edges = [
            (r["task_id"], r["depends_on_task_id"])
            for r in await _project_edges(conn, projects[0]["pid"])
        ]
        if engine.creates_cycle(edges, task_id, dep):
            raise HTTPException(status_code=409, detail="That link would create a loop")
        await conn.execute(
            "INSERT INTO task_dependencies (task_id, depends_on_task_id, source) VALUES ($1, $2, 'manual')"
            " ON CONFLICT DO NOTHING",
            task_id,
            dep,
        )
    return {"task_id": str(task_id), "depends_on": str(dep)}


@router.delete("/tasks/{task_id}/dependencies/{depends_on}")
async def remove_dependency(
    task_id: UUID,
    depends_on: UUID,
    _user: CurrentUser = Depends(require_role(*MANAGEMENT_ROLES)),
) -> dict:
    async with acquire() as conn:
        await ensure_in_scope(conn, "task", task_id)
        result = await conn.execute(
            "DELETE FROM task_dependencies WHERE task_id = $1 AND depends_on_task_id = $2", task_id, depends_on
        )
    if result.endswith(" 0"):
        raise HTTPException(status_code=404, detail="Dependency not found")
    return {"deleted": True}


@router.post("/projects/{project_id}/milestones", status_code=201)
async def create_milestone(
    project_id: UUID,
    body: dict = Body(...),
    _user: CurrentUser = Depends(require_role(*MANAGEMENT_ROLES)),
) -> dict:
    name = (body.get("name") or "").strip()
    due = _parse_date(body.get("due_date"), "due_date")
    kind = body.get("kind") or "milestone"
    if not name or not due:
        raise HTTPException(status_code=400, detail="name and due_date are required")
    if kind not in ("milestone", "review", "delivery"):
        raise HTTPException(status_code=400, detail="kind must be milestone, review or delivery")
    async with acquire() as conn:
        await _project(conn, project_id)
        row = await conn.fetchrow(
            "INSERT INTO milestones (project_id, name, due_date, kind, description)"
            " VALUES ($1, $2, $3, $4, $5) RETURNING *",
            project_id,
            name,
            due,
            kind,
            (body.get("description") or "").strip() or None,
        )
    return _milestone(row)


@router.patch("/milestones/{milestone_id}")
async def update_milestone(
    milestone_id: UUID,
    body: dict = Body(...),
    _user: CurrentUser = Depends(require_role(*MANAGEMENT_ROLES)),
) -> dict:
    sets, params = [], []
    if "name" in body:
        if not (body["name"] or "").strip():
            raise HTTPException(status_code=400, detail="name cannot be empty")
        params.append(body["name"].strip())
        sets.append(f"name = ${len(params)}")
    if "due_date" in body:
        params.append(_parse_date(body["due_date"], "due_date"))
        if params[-1] is None:
            raise HTTPException(status_code=400, detail="due_date is required")
        sets.append(f"due_date = ${len(params)}")
    if "kind" in body:
        if body["kind"] not in ("milestone", "review", "delivery"):
            raise HTTPException(status_code=400, detail="kind must be milestone, review or delivery")
        params.append(body["kind"])
        sets.append(f"kind = ${len(params)}")
    if "description" in body:
        params.append((body["description"] or "").strip() or None)
        sets.append(f"description = ${len(params)}")
    if "done" in body:
        params.append(datetime.now(timezone.utc) if body["done"] else None)
        sets.append(f"done_at = ${len(params)}")
    if not sets:
        raise HTTPException(status_code=400, detail="No updatable fields provided")
    params.append(milestone_id)
    async with acquire() as conn:
        await ensure_in_scope(conn, "milestone", milestone_id)
        row = await conn.fetchrow(
            f"UPDATE milestones SET {', '.join(sets)} WHERE id = ${len(params)} RETURNING *", *params
        )
    if row is None:
        raise HTTPException(status_code=404, detail="Milestone not found")
    return _milestone(row)


@router.delete("/milestones/{milestone_id}")
async def delete_milestone(
    milestone_id: UUID, _user: CurrentUser = Depends(require_role(*MANAGEMENT_ROLES))
) -> dict:
    async with acquire() as conn:
        await ensure_in_scope(conn, "milestone", milestone_id)
        deleted = await conn.fetchval("DELETE FROM milestones WHERE id = $1 RETURNING id", milestone_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Milestone not found")
    return {"deleted": str(deleted)}
