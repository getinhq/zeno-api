"""CAS HTTP API: POST/PUT/GET/HEAD /api/v1/cas/blobs; GET .../exists for dedup."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Union

from blake3 import blake3
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from app.cas.cache import get_cached_exists, set_cached_exists
from app.cas.factory import get_cas_backend, is_cas_configured
from app.cas.paths import is_valid_hash

router = APIRouter(prefix="/api/v1/cas", tags=["cas"])


def _validate_hash(hash_str: str) -> None:
    if not is_valid_hash(hash_str):
        raise HTTPException(status_code=400, detail="Invalid hash: must be 64 lowercase hex characters")


@router.post("/blobs")
async def post_blob(request: Request) -> Response:
    """Upload blob with X-Content-Hash header; stream body, verify BLAKE3. 201 created, 200 if exists, 400 on mismatch."""
    if not is_cas_configured():
        return Response(status_code=503, content="CAS not configured (S3 or ZENO_CAS_ROOT)")
    raw = request.headers.get("X-Content-Hash", "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="Missing or empty X-Content-Hash header")
    hash_str = raw.lower()
    _validate_hash(hash_str)
    try:
        backend = get_cas_backend()
    except RuntimeError:
        return Response(status_code=503, content="CAS not configured")
    tmp_dir = backend.ensure_tmp()
    hasher = blake3()
    fd, tmp_path = tempfile.mkstemp(dir=tmp_dir, prefix="blob_")
    try:
        with os.fdopen(fd, "wb") as f:
            async for chunk in request.stream():
                hasher.update(chunk)
                f.write(chunk)
        computed = hasher.hexdigest()
        if computed != hash_str:
            os.unlink(tmp_path)
            raise HTTPException(
                status_code=400,
                detail=f"Content hash mismatch: expected {hash_str[:16]}..., got {computed[:16]}...",
            )
        created = backend.put_from_path(hash_str, Path(tmp_path))
        return Response(status_code=201 if created else 200)
    except HTTPException:
        raise
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise


@router.get("/blobs/{hash_str}/exists", response_model=None)
async def blob_exists(hash_str: str) -> Union[Response, dict]:
    """Return 200 with {\"exists\": true} if blob exists, 404 if not. For client-side dedup."""
    if not is_cas_configured():
        return Response(status_code=503, content="CAS not configured (S3 or ZENO_CAS_ROOT)")
    _validate_hash(hash_str)
    try:
        backend = get_cas_backend()
    except RuntimeError:
        return Response(status_code=503, content="CAS not configured")
    cached = None
    try:
        cached = await get_cached_exists(hash_str)
    except Exception:
        cached = None
    if cached is not None:
        if not cached:
            raise HTTPException(status_code=404, detail="Blob not found")
        return {"exists": True}
    exists = backend.exists(hash_str)
    try:
        await set_cached_exists(hash_str, exists)
    except Exception:
        pass
    if not exists:
        raise HTTPException(status_code=404, detail="Blob not found")
    return {"exists": True}


@router.put("/blobs/{hash_str:path}")
async def put_blob(hash_str: str, request: Request) -> Response:
    """Stream body to CAS; verify BLAKE3; 400 if mismatch, 201 created, 200 if exists."""
    if not is_cas_configured():
        return Response(status_code=503, content="CAS not configured (S3 or ZENO_CAS_ROOT)")
    _validate_hash(hash_str)
    try:
        backend = get_cas_backend()
    except RuntimeError:
        return Response(status_code=503, content="CAS not configured")
    tmp_dir = backend.ensure_tmp()
    hasher = blake3()
    fd, tmp_path = tempfile.mkstemp(dir=tmp_dir, prefix="blob_")
    try:
        with os.fdopen(fd, "wb") as f:
            async for chunk in request.stream():
                hasher.update(chunk)
                f.write(chunk)
        computed = hasher.hexdigest()
        if computed != hash_str:
            os.unlink(tmp_path)
            return Response(
                status_code=400,
                content=f"Content hash mismatch: expected {hash_str[:16]}..., got {computed[:16]}...",
            )
        created = backend.put_from_path(hash_str, Path(tmp_path))
        return Response(status_code=201 if created else 200)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise


@router.get("/blobs/{hash_str:path}")
async def get_blob(hash_str: str):
    """Stream blob bytes; 404 if not found."""
    if not is_cas_configured():
        return Response(status_code=503, content="CAS not configured (S3 or ZENO_CAS_ROOT)")
    _validate_hash(hash_str)
    try:
        backend = get_cas_backend()
    except RuntimeError:
        return Response(status_code=503, content="CAS not configured")
    if not backend.exists(hash_str):
        raise HTTPException(status_code=404, detail="Blob not found")

    def gen():
        yield from backend.get_stream(hash_str)

    return StreamingResponse(gen(), media_type="application/octet-stream")


@router.head("/blobs/{hash_str:path}")
async def head_blob(hash_str: str) -> Response:
    """200 if exists with Content-Length; 404 if not found."""
    if not is_cas_configured():
        return Response(status_code=503, content="CAS not configured (S3 or ZENO_CAS_ROOT)")
    _validate_hash(hash_str)
    try:
        backend = get_cas_backend()
    except RuntimeError:
        return Response(status_code=503, content="CAS not configured")
    if not backend.exists(hash_str):
        return Response(status_code=404)
    size = backend.get_size(hash_str)
    return Response(status_code=200, headers={"Content-Length": str(size)})
