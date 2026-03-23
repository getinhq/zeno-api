"""Register-Version API: POST /api/v1/versions to link CAS content to a DB version row."""
from __future__ import annotations

import json
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Path, Query, Response
from pydantic import BaseModel, Field, validator

from app.cas.factory import get_cas_backend
from app.cas.paths import is_valid_hash
from app.config import MONGO_URI
from app.manifests.store import get_manifest_document
from app.db import acquire
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
    content_id: str = Field(..., description="64-char SHA-256 hex CAS content id")
    filename: Optional[str] = Field(None, description="Optional human-facing filename")
    size: Optional[int] = Field(None, description="Optional size in bytes")
    publish_batch_id: Optional[str] = Field(
        None, description="Optional UUID to group multiple representations into one version number"
    )

    @validator("content_id")
    def validate_content_id(cls, v: str) -> str:
        v = v.strip().lower()
        if not is_valid_hash(v):
            raise ValueError("content_id must be a 64-character lowercase hex SHA-256 hash")
        return v

    @validator("version")
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


@router.post("/versions", response_model=RegisteredVersionResponse, status_code=201)
async def register_version_endpoint(body: RegisterVersionRequest) -> Any:
    """Register a new version for an existing asset, linked to an existing CAS blob."""
    data = RegisterVersionData(
        project=body.project,
        asset=body.asset,
        representation=body.representation,
        version=body.version,
        content_id=body.content_id,
        filename=body.filename,
        size=body.size,
        publish_batch_id=body.publish_batch_id,
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
    return result


class VersionRepresentation(BaseModel):
    version_id: str
    representation: str
    content_id: str
    filename: str
    size: Optional[int]
    publish_batch_id: Optional[str] = None
    published_at: Optional[str] = None


class AssetVersionGroup(BaseModel):
    version_number: int
    publish_batch_id: Optional[str] = None
    published_at: Optional[str] = None
    representations: list[VersionRepresentation]


@router.get("/assets/{asset_id}/versions", response_model=list[AssetVersionGroup])
async def list_versions_for_asset(asset_id: UUID = Path(...)) -> Any:
    """
    List all versions for an asset, grouped by version_number.
    Each group includes multiple representations (fbx, abc, blend, etc.) if they exist.
    """
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, representation, version_number, content_id, filename, size_bytes, publish_batch_id, published_at
            FROM versions
            WHERE asset_id = $1
            ORDER BY version_number DESC, representation ASC
            """,
            asset_id,
        )
    groups: dict[tuple[int, str | None], dict] = {}
    for r in rows:
        vb = str(r["publish_batch_id"]) if r["publish_batch_id"] else None
        key = (int(r["version_number"]), vb)
        if key not in groups:
            groups[key] = {
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
                "publish_batch_id": vb,
                "published_at": r["published_at"].isoformat() if r["published_at"] else None,
            }
        )
    # stable order: version desc
    out = list(groups.values())
    out.sort(key=lambda g: int(g["version_number"]), reverse=True)
    return out


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
    Resolve a manifest by content hash: MongoDB first (chimera.manifest.v1), else legacy CAS blob.
    """
    cid = content_id.strip().lower()
    if not is_valid_hash(cid):
        raise HTTPException(status_code=400, detail="content_id must be a 64-character lowercase hex SHA-256 hash")

    if MONGO_URI:
        try:
            doc = get_manifest_document(cid)
            if doc is not None:
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
    return j

