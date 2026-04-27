"""Service layer for registering asset versions linked to CAS content."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional

import asyncpg

from app.cas.factory import get_cas_backend
from app.cas.paths import is_valid_hash
from app.db import acquire


class NotFound(Exception):
    """Raised when a referenced project or asset does not exist."""


class VersionConflict(Exception):
    """Raised when an explicit version_number already exists."""


class ContentNotFoundInCas(Exception):
    """Raised when the given CAS content_id does not exist."""


class ServiceUnavailable(Exception):
    """Raised when an infrastructure dependency (DB/CAS) is unavailable."""


_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def is_uuid_like(value: str) -> bool:
    return bool(_UUID_RE.match(value))


@dataclass
class RegisterVersionData:
    project: str
    asset: str
    representation: str
    version: str
    content_id: str
    filename: Optional[str]
    size: Optional[int]
    publish_batch_id: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None
    pipeline_stage: str = ""
    task_id: Optional[str] = None
    feedback: Optional[str] = None
    status: str = "pending"


async def _resolve_project_id(conn: asyncpg.Connection, project_spec: str) -> Optional[str]:
    if is_uuid_like(project_spec):
        row = await conn.fetchrow("SELECT id FROM projects WHERE id = $1", project_spec)
    else:
        row = await conn.fetchrow("SELECT id FROM projects WHERE code = $1", project_spec)
    return str(row["id"]) if row else None


async def _resolve_asset_id(
    conn: asyncpg.Connection, project_id: str, asset_spec: str
) -> Optional[str]:
    if is_uuid_like(asset_spec):
        row = await conn.fetchrow(
            "SELECT id FROM assets WHERE id = $1 AND project_id = $2", asset_spec, project_id
        )
    else:
        row = await conn.fetchrow(
            "SELECT id FROM assets WHERE code = $1 AND project_id = $2", asset_spec, project_id
        )
    return str(row["id"]) if row else None


async def _pick_version_number(
    conn: asyncpg.Connection,
    asset_id: str,
    representation: str,
    version_spec: str,
    *,
    pipeline_stage: str = "",
    publish_batch_id: Optional[str] = None,
) -> tuple[int, bool]:
    """Return (version_number, is_explicit)."""
    if version_spec == "next":
        # If publish_batch_id is provided, ensure all representations in the same batch
        # share the same version_number for this asset.
        if publish_batch_id:
            row = await conn.fetchrow(
                """
                SELECT version_number
                FROM versions
                WHERE asset_id = $1 AND publish_batch_id = $2 AND pipeline_stage = $3
                ORDER BY created_at ASC
                LIMIT 1
                """,
                asset_id,
                publish_batch_id,
                pipeline_stage,
            )
            if row and row["version_number"] is not None:
                return int(row["version_number"]), False
            row = await conn.fetchrow(
                """
                SELECT max(version_number) AS maxver
                FROM versions
                WHERE asset_id = $1 AND pipeline_stage = $2
                """,
                asset_id,
                pipeline_stage,
            )
            maxver = row["maxver"] if row and row["maxver"] is not None else None
            return ((maxver + 1) if maxver is not None else 1), False

        row = await conn.fetchrow(
            """
            SELECT max(version_number) AS maxver
            FROM versions
            WHERE asset_id = $1 AND representation = $2 AND pipeline_stage = $3
            """,
            asset_id,
            representation,
            pipeline_stage,
        )
        maxver = row["maxver"] if row and row["maxver"] is not None else None
        return ((maxver + 1) if maxver is not None else 1), False
    try:
        num = int(version_spec)
    except (TypeError, ValueError) as e:
        raise ValueError("version must be 'next' or a positive integer") from e
    if num <= 0:
        raise ValueError("version must be a positive integer")
    return num, True


def _ensure_cas_content_exists(content_id: str) -> None:
    try:
        backend = get_cas_backend()
    except RuntimeError as e:
        raise ServiceUnavailable(str(e)) from e
    if not backend.exists(content_id):
        raise ContentNotFoundInCas(f"CAS content not found for hash {content_id[:16]}...")


def _ensure_optional_dedup_from_metadata(metadata: Optional[dict[str, Any]]) -> None:
    """If metadata links a dedup artifact, ensure that CAS blob exists too."""
    if not metadata:
        return
    da = metadata.get("dedup_artifact")
    if not isinstance(da, dict):
        return
    did = str(da.get("content_id") or "").strip().lower()
    if did and is_valid_hash(did):
        _ensure_cas_content_exists(did)


async def register_version(data: RegisterVersionData) -> dict[str, Any]:
    """Register a new version row for an existing asset, linked to an existing CAS blob."""
    content_id = data.content_id
    if not is_valid_hash(content_id):
        raise ValueError("content_id must be a 64-character lowercase hex hash")

    pipeline_stage = (data.pipeline_stage or "").strip().lower()
    if pipeline_stage not in {"", "modelling", "texturing", "rigging", "lookdev"}:
        raise ValueError(
            "pipeline_stage must be empty (legacy) or one of: modelling, texturing, rigging, lookdev"
        )
    version_status = (data.status or "pending").strip().lower()
    if version_status not in {"pending", "in_review", "approved", "rejected"}:
        raise ValueError("status must be one of: pending, in_review, approved, rejected")

    async with acquire() as conn:
        try:
            async with conn.transaction():
                project_id = await _resolve_project_id(conn, data.project)
                if project_id is None:
                    raise NotFound("project")

                asset_id = await _resolve_asset_id(conn, project_id, data.asset)
                if asset_id is None:
                    raise NotFound("asset")

                version_number, explicit = await _pick_version_number(
                    conn,
                    asset_id,
                    data.representation,
                    data.version,
                    pipeline_stage=pipeline_stage,
                    publish_batch_id=data.publish_batch_id,
                )

                if explicit:
                    exists_row = await conn.fetchrow(
                        """
                        SELECT 1
                        FROM versions
                        WHERE asset_id = $1 AND representation = $2 AND version_number = $3
                          AND pipeline_stage = $4
                        """,
                        asset_id,
                        data.representation,
                        version_number,
                        pipeline_stage,
                    )
                    if exists_row:
                        raise VersionConflict(
                            f"Version {version_number} already exists for asset and representation"
                        )

                # Check CAS before inserting row (delivery + optional dedup manifest)
                _ensure_cas_content_exists(content_id)
                _ensure_optional_dedup_from_metadata(data.metadata)

                # Use provided filename or fall back to content_id
                filename = data.filename or content_id
                metadata_json = json.dumps(data.metadata) if data.metadata is not None else None

                row = await conn.fetchrow(
                    """
                    INSERT INTO versions (
                        asset_id,
                        representation,
                        pipeline_stage,
                        version_number,
                        content_id,
                        filename,
                        size_bytes,
                        task_id,
                        publish_batch_id,
                        metadata,
                        feedback,
                        status
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8::uuid, $9::uuid, $10::jsonb, $11, $12)
                    RETURNING id, asset_id, representation, pipeline_stage, version_number, content_id, filename, size_bytes, task_id, metadata, feedback, status
                    """,
                    asset_id,
                    data.representation,
                    pipeline_stage,
                    version_number,
                    content_id,
                    filename,
                    data.size,
                    data.task_id,
                    data.publish_batch_id,
                    metadata_json,
                    data.feedback,
                    version_status,
                )
        except asyncpg.PostgresError as e:
            raise ServiceUnavailable(f"Database error: {str(e)}") from e

    out: dict[str, Any] = {
        "project_id": project_id,
        "asset_id": str(row["asset_id"]),
        "version_id": str(row["id"]),
        "version_number": row["version_number"],
        "content_id": row["content_id"],
        "filename": row["filename"],
        "size": row["size_bytes"],
        "task_id": str(row["task_id"]) if row["task_id"] else None,
        "feedback": row["feedback"],
        "status": row["status"],
    }
    meta = row["metadata"]
    if meta is not None:
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = None
        if isinstance(meta, dict):
            out["metadata"] = meta
    return out

