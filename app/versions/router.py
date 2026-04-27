"""Register-Version API: POST /api/v1/versions to link CAS content to a DB version row."""
from __future__ import annotations

import json
from typing import Any, Dict, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response
from pydantic import BaseModel, Field, field_validator

from app.auth.deps import CurrentUser, MANAGEMENT_ROLES, optional_current_user
from app.cas.factory import get_cas_backend
from app.cas.paths import is_valid_hash
from app.config import MONGO_URI
from app.events import log as events_log
from app.manifests.cache import get_cached_manifest, set_cached_manifest
from app.manifests.store import get_manifest_document
from app.db import acquire
from app.notifications import service as notifications_service
from app.versions.service import (
    ContentNotFoundInCas,
    NotFound,
    RegisterVersionData,
    ServiceUnavailable,
    VersionConflict,
    register_version,
)

router = APIRouter(prefix="/api/v1", tags=["versions"])


class RegisterVersionRequest(BaseModel):
    project: str = Field(..., description="Project code or UUID")
    asset: str = Field(..., description="Asset code or UUID within the project")
    representation: str = Field(..., description="Representation key, e.g. model, fbx, usd")
    version: str = Field(..., description="'next' or an explicit positive integer as string")
    content_id: str = Field(..., description="64-char lowercase hex CAS content id")
    filename: Optional[str] = Field(None, description="Optional human-facing filename")
    size: Optional[int] = Field(None, description="Optional size in bytes")
    publish_batch_id: Optional[str] = Field(
        None, description="Optional UUID to group multiple representations into one version number"
    )
    metadata: Optional[Dict[str, Any]] = Field(
        None,
        description="Optional JSON metadata (e.g. dedup_artifact link for dual-artifact publishes)",
    )
    pipeline_stage: Optional[str] = Field(
        None,
        description="Asset pipeline stage (modelling, texturing, rigging, lookdev); omit or empty for legacy/global",
    )
    task_id: Optional[str] = Field(
        None,
        description="Optional task UUID this version is published for",
    )
    feedback: Optional[str] = Field(None, description="Optional review/publish feedback text")
    status: Optional[str] = Field(
        "pending",
        description="Version status: pending, in_review, approved, rejected",
    )

    @field_validator("content_id")
    @classmethod
    def validate_content_id(cls, v: str) -> str:
        v = v.strip().lower()
        if not is_valid_hash(v):
            raise ValueError("content_id must be a 64-character lowercase hex hash")
        return v

    @field_validator("version")
    @classmethod
    def validate_version(cls, v: str) -> str:
        v = v.strip()
        if v == "next":
            return v
        # allow simple integer in string form; detailed validation happens in service
        if not v.isdigit():
            raise ValueError("version must be 'next' or a positive integer")
        return v


class RegisteredVersionResponse(BaseModel):
    project_id: str
    asset_id: str
    version_id: str
    version_number: int
    content_id: str
    filename: str
    size: Optional[int]
    feedback: Optional[str] = None
    status: str
    metadata: Optional[Dict[str, Any]] = None
    task_id: Optional[str] = None


async def _validate_publish_task_access(
    conn,
    *,
    task_id: UUID,
    project_id: str,
    asset_id: str,
    current: Optional[CurrentUser],
) -> None:
    row = await conn.fetchrow(
        """
        SELECT id, project_id, asset_id, assignee_id
        FROM tasks
        WHERE id = $1
        """,
        task_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")

    task_project_id = str(row["project_id"]) if row["project_id"] else None
    task_asset_id = str(row["asset_id"]) if row["asset_id"] else None
    if task_project_id and task_project_id != project_id:
        raise HTTPException(status_code=400, detail="task_id does not belong to target project")
    if task_asset_id and task_asset_id != asset_id:
        raise HTTPException(status_code=400, detail="task_id does not belong to target asset")

    if current is None:
        return
    role = (current.app_role or "").lower()
    if role in MANAGEMENT_ROLES:
        return
    allowed = await conn.fetchval(
        """
        SELECT EXISTS(
            SELECT 1
            FROM tasks t
            WHERE t.id = $1
              AND (
                t.assignee_id = $2
                OR EXISTS (SELECT 1 FROM task_assignees ta WHERE ta.task_id = t.id AND ta.user_id = $2)
                OR EXISTS (SELECT 1 FROM task_collaborators tc WHERE tc.task_id = t.id AND tc.user_id = $2)
              )
        )
        """,
        task_id,
        current.id,
    )
    if not bool(allowed):
        raise HTTPException(
            status_code=403,
            detail="You can publish only for tasks assigned to you or where you're a collaborator",
        )


async def _set_task_status_for_publish(conn, *, task_id: UUID, actor_id: Optional[UUID]) -> None:
    updated = await conn.fetchrow(
        """
        UPDATE tasks
        SET status = 'review', updated_at = NOW()
        WHERE id = $1
        RETURNING id, project_id
        """,
        task_id,
    )
    if not updated:
        return
    await events_log.emit(
        conn,
        project_id=updated["project_id"],
        actor_id=actor_id,
        kind="task.status.changed",
        payload={"task_id": str(task_id), "to": "in_review", "source": "version.published"},
    )


async def _sync_task_status_from_latest_version(conn, *, task_id: UUID, actor_id: Optional[UUID]) -> None:
    latest = await conn.fetchrow(
        """
        SELECT status
        FROM versions
        WHERE task_id = $1
        ORDER BY version_number DESC, created_at DESC
        LIMIT 1
        """,
        task_id,
    )
    if not latest:
        return
    latest_status = str(latest["status"] or "").strip().lower()
    next_task_status = "done" if latest_status == "approved" else "in_progress"
    updated = await conn.fetchrow(
        """
        UPDATE tasks
        SET status = $2, updated_at = NOW()
        WHERE id = $1
        RETURNING id, project_id
        """,
        task_id,
        next_task_status,
    )
    if not updated:
        return
    display_status = "completed" if next_task_status == "done" else next_task_status
    await events_log.emit(
        conn,
        project_id=updated["project_id"],
        actor_id=actor_id,
        kind="task.status.changed",
        payload={"task_id": str(task_id), "to": display_status, "source": "version.status.sync"},
    )


@router.post("/versions", response_model=RegisteredVersionResponse, status_code=201)
async def register_version_endpoint(
    body: RegisterVersionRequest,
    current: Optional[CurrentUser] = Depends(optional_current_user),
) -> Any:
    """Register a new version for an existing asset, linked to an existing CAS blob."""
    task_uuid: Optional[UUID] = None
    if body.task_id:
        try:
            task_uuid = UUID(str(body.task_id))
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail="task_id must be a valid UUID") from exc
    data = RegisterVersionData(
        project=body.project,
        asset=body.asset,
        representation=body.representation,
        version=body.version,
        content_id=body.content_id,
        filename=body.filename,
        size=body.size,
        publish_batch_id=body.publish_batch_id,
        metadata=body.metadata,
        pipeline_stage=(body.pipeline_stage or "").strip().lower(),
        task_id=str(task_uuid) if task_uuid else None,
        feedback=body.feedback,
        status=(body.status or "pending").strip().lower(),
    )
    if task_uuid is not None:
        async with acquire() as conn:
            project_row = await conn.fetchrow("SELECT id FROM projects WHERE code = $1 OR id::text = $1", body.project)
            if not project_row:
                raise HTTPException(status_code=404, detail="Project not found")
            asset_row = await conn.fetchrow(
                """
                SELECT a.id
                FROM assets a
                WHERE (a.code = $1 OR a.id::text = $1)
                  AND a.project_id = $2
                """,
                body.asset,
                project_row["id"],
            )
            if not asset_row:
                raise HTTPException(status_code=404, detail="Asset not found")
            await _validate_publish_task_access(
                conn,
                task_id=task_uuid,
                project_id=str(project_row["id"]),
                asset_id=str(asset_row["id"]),
                current=current,
            )
    try:
        result = await register_version(data)
    except NotFound as e:
        subject = str(e)
        if subject == "project":
            raise HTTPException(status_code=404, detail="Project not found") from e
        if subject == "asset":
            raise HTTPException(status_code=404, detail="Asset not found") from e
        raise HTTPException(status_code=404, detail="Not found") from e
    except VersionConflict as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ContentNotFoundInCas as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except ServiceUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Register version error: {str(e)}") from e

    try:
        async with acquire() as conn:
            if task_uuid is not None:
                await _set_task_status_for_publish(
                    conn,
                    task_id=task_uuid,
                    actor_id=getattr(current, "id", None),
                )
            payload = {
                "version_id": result.version_id,
                "asset_id": result.asset_id,
                "version_number": result.version_number,
                "filename": result.filename,
                "representation": data.representation,
                "pipeline_stage": data.pipeline_stage,
                "task_id": str(task_uuid) if task_uuid else None,
            }
            await events_log.emit(
                conn,
                project_id=result.project_id,
                actor_id=None,
                kind="version.published",
                payload=payload,
            )
            await notifications_service.emit(
                conn,
                project_id=result.project_id,
                kind="version.published",
                payload=payload,
                audience="project",
            )
    except Exception:  # noqa: BLE001
        pass
    return result


class VersionRepresentation(BaseModel):
    version_id: str
    representation: str
    content_id: str
    filename: str
    size: Optional[int]
    feedback: Optional[str] = None
    status: Optional[str] = None
    publish_batch_id: Optional[str] = None
    published_at: Optional[str] = None


class UpdateVersionRequest(BaseModel):
    feedback: Optional[str] = None
    status: Optional[str] = None


class AssetVersionGroup(BaseModel):
    pipeline_stage: str = ""
    version_number: int
    publish_batch_id: Optional[str] = None
    published_at: Optional[str] = None
    representations: list[VersionRepresentation]


class LatestContentResponse(BaseModel):
    content_id: str
    version_number: int
    representation: str


@router.get("/assets/{asset_id}/versions", response_model=list[AssetVersionGroup])
async def list_versions_for_asset(
    asset_id: UUID = Path(...),
    pipeline_stage: str = Query(
        "",
        description="Filter by asset pipeline stage; empty string = legacy publishes without a stage",
    ),
) -> Any:
    """
    List versions for an asset, grouped by version_number (within the selected pipeline_stage).
    Each group includes multiple representations (fbx, abc, blend, etc.) if they exist.
    """
    stage = pipeline_stage.strip().lower()
    if stage not in {"", "modelling", "texturing", "rigging", "lookdev"}:
        raise HTTPException(status_code=400, detail="Invalid pipeline_stage filter")

    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, representation, pipeline_stage, version_number, content_id, filename, size_bytes,
                   feedback, status,
                   publish_batch_id, published_at
            FROM versions
            WHERE asset_id = $1 AND pipeline_stage = $2
            ORDER BY version_number DESC, representation ASC
            """,
            asset_id,
            stage,
        )
    groups: dict[tuple[int, str | None], dict] = {}
    for r in rows:
        vb = str(r["publish_batch_id"]) if r["publish_batch_id"] else None
        key = (int(r["version_number"]), vb)
        if key not in groups:
            groups[key] = {
                "pipeline_stage": str(r["pipeline_stage"] or ""),
                "version_number": int(r["version_number"]),
                "publish_batch_id": vb,
                "published_at": r["published_at"].isoformat() if r["published_at"] else None,
                "representations": [],
            }
        groups[key]["representations"].append(
            {
                "version_id": str(r["id"]),
                "representation": r["representation"],
                "content_id": r["content_id"],
                "filename": r["filename"],
                "size": r["size_bytes"],
                "feedback": r["feedback"],
                "status": r["status"],
                "publish_batch_id": vb,
                "published_at": r["published_at"].isoformat() if r["published_at"] else None,
            }
        )
    # stable order: version desc
    out = list(groups.values())
    out.sort(key=lambda g: int(g["version_number"]), reverse=True)
    return out


@router.patch("/versions/{version_id}")
async def update_version(
    body: UpdateVersionRequest,
    version_id: UUID = Path(...),
    current: Optional[CurrentUser] = Depends(optional_current_user),
) -> Any:
    fields: list[str] = []
    params: list[Any] = []
    if body.feedback is not None:
        fields.append(f"feedback = ${len(params) + 1}")
        params.append(body.feedback)
    if body.status is not None:
        status = body.status.strip().lower()
        if status not in {"pending", "in_review", "approved", "rejected"}:
            raise HTTPException(status_code=400, detail="Invalid status")
        fields.append(f"status = ${len(params) + 1}")
        params.append(status)
    if not fields:
        raise HTTPException(status_code=400, detail="No updatable fields provided")
    params.append(version_id)
    query = (
        "UPDATE versions SET "
        + ", ".join(fields)
        + " WHERE id = $"
        + str(len(params))
        + " RETURNING id, feedback, status"
    )
    async with acquire() as conn:
        row = await conn.fetchrow(
            query.replace("RETURNING id, feedback, status", "RETURNING id, task_id, feedback, status"),
            *params,
        )
        if row and (body.status is not None or body.feedback is not None):
            await events_log.emit(
                conn,
                project_id=await conn.fetchval(
                    """
                    SELECT p.id
                    FROM versions v
                    JOIN assets a ON a.id = v.asset_id
                    JOIN projects p ON p.id = a.project_id
                    WHERE v.id = $1
                    """,
                    version_id,
                ),
                actor_id=getattr(current, "id", None),
                kind="version.updated",
                payload={
                    "version_id": str(version_id),
                    "status": row["status"],
                    "feedback": row["feedback"],
                    "task_id": str(row["task_id"]) if row["task_id"] else None,
                },
            )
        if row and row["task_id"] and body.status is not None:
            await _sync_task_status_from_latest_version(
                conn,
                task_id=row["task_id"],
                actor_id=getattr(current, "id", None),
            )
    if not row:
        raise HTTPException(status_code=404, detail="Version not found")
    return {"version_id": str(row["id"]), "feedback": row["feedback"], "status": row["status"]}


@router.get("/versions/latest-content", response_model=LatestContentResponse)
async def latest_content_id(
    project: str = Query(..., description="Project code or UUID"),
    asset: str = Query(..., description="Asset code or UUID"),
    representation: str = Query(..., description="Representation key"),
    pipeline_stage: str = Query(
        "",
        description="Asset pipeline stage; empty = legacy/global publishes",
    ),
    artifact: Literal["delivery", "dedup"] = Query(
        "delivery",
        description="delivery=primary CAS blob (default resolver); dedup=canonical manifest id from metadata when present",
    ),
) -> Any:
    """
    Return latest content_id for project/asset/representation.
    Used by Omni-Chunker to resolve parent version for patching (use artifact=dedup when dual-artifact).
    """
    stage = pipeline_stage.strip().lower()
    if stage not in {"", "modelling", "texturing", "rigging", "lookdev"}:
        raise HTTPException(status_code=400, detail="Invalid pipeline_stage")

    async with acquire() as conn:
        if artifact == "dedup":
            row = await conn.fetchrow(
                """
                SELECT
                    COALESCE(
                        NULLIF(trim(lower(v.metadata->'dedup_artifact'->>'content_id')), ''),
                        v.content_id
                    ) AS content_id,
                    v.version_number,
                    v.representation
                FROM versions v
                JOIN assets a ON a.id = v.asset_id
                JOIN projects p ON p.id = a.project_id
                WHERE
                    (p.code = $1 OR p.id::text = $1)
                    AND (a.code = $2 OR a.id::text = $2)
                    AND v.representation = $3
                    AND v.pipeline_stage = $4
                ORDER BY v.version_number DESC
                LIMIT 1
                """,
                project,
                asset,
                representation,
                stage,
            )
        else:
            row = await conn.fetchrow(
                """
                SELECT v.content_id, v.version_number, v.representation
                FROM versions v
                JOIN assets a ON a.id = v.asset_id
                JOIN projects p ON p.id = a.project_id
                WHERE
                    (p.code = $1 OR p.id::text = $1)
                    AND (a.code = $2 OR a.id::text = $2)
                    AND v.representation = $3
                    AND v.pipeline_stage = $4
                ORDER BY v.version_number DESC
                LIMIT 1
                """,
                project,
                asset,
                representation,
                stage,
            )
    if not row:
        raise HTTPException(status_code=404, detail="No version found for project/asset/representation")
    return {
        "content_id": str(row["content_id"]).strip().lower(),
        "version_number": int(row["version_number"]),
        "representation": str(row["representation"]),
    }


def _cas_backend():
    try:
        return get_cas_backend()
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@router.get("/manifests/{content_id}")
async def get_manifest_json(
    content_id: str,
    max_bytes: int = Query(1024 * 1024, ge=1, le=10 * 1024 * 1024),
) -> Any:
    """
    Resolve a manifest by content hash: Redis cache -> MongoDB -> legacy CAS blob.
    """
    cid = content_id.strip().lower()
    if not is_valid_hash(cid):
        raise HTTPException(status_code=400, detail="content_id must be a 64-character lowercase hex hash")

    try:
        cached = await get_cached_manifest(cid)
        if cached is not None:
            return cached
    except Exception:
        pass

    if MONGO_URI:
        try:
            doc = get_manifest_document(cid)
            if doc is not None:
                try:
                    await set_cached_manifest(cid, doc)
                except Exception:
                    pass
                return doc
        except Exception:
            pass

    backend = _cas_backend()
    if not backend.exists(cid):
        raise HTTPException(status_code=404, detail="Manifest not found")
    size = backend.get_size(cid)
    if size > max_bytes:
        raise HTTPException(status_code=413, detail=f"Manifest too large ({size} bytes)")
    b = b"".join(backend.get_stream(cid))
    try:
        j = json.loads(b.decode("utf-8"))
    except Exception as e:
        raise HTTPException(status_code=400, detail="Blob is not valid JSON") from e
    if isinstance(j, dict):
        try:
            await set_cached_manifest(cid, j)
        except Exception:
            pass
    return j

