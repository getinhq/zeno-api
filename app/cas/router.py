"""CAS HTTP API: PUT/GET/HEAD /api/v1/cas/blobs/<hash>."""
import hashlib
import os
import tempfile
from pathlib import Path

from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse
from fastapi import HTTPException

from app.cas.backend import NASBackend
from app.cas.paths import is_valid_hash
from app.config import CAS_ROOT

router = APIRouter(prefix="/api/v1/cas", tags=["cas"])


def _get_backend() -> NASBackend:
    if not CAS_ROOT:
        raise RuntimeError("CAS_ROOT not configured")
    return NASBackend(CAS_ROOT)


def _validate_hash(hash_str: str) -> None:
    if not is_valid_hash(hash_str):
        raise HTTPException(status_code=400, detail="Invalid hash: must be 64 lowercase hex characters")


@router.put("/blobs/{hash_str:path}")
async def put_blob(hash_str: str, request: Request) -> Response:
    """Stream body to CAS; verify SHA-256; 400 if mismatch, 201 created, 200 if exists."""
    if not CAS_ROOT:
        return Response(status_code=503, content="CAS not configured (ZENO_CAS_ROOT)")
    _validate_hash(hash_str)
    backend = _get_backend()
    tmp_dir = backend._ensure_tmp()
    hasher = hashlib.sha256()
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
    if not CAS_ROOT:
        return Response(status_code=503, content="CAS not configured (ZENO_CAS_ROOT)")
    _validate_hash(hash_str)
    backend = _get_backend()
    if not backend.exists(hash_str):
        raise HTTPException(status_code=404, detail="Blob not found")

    def gen():
        yield from backend.get_stream(hash_str)

    return StreamingResponse(gen(), media_type="application/octet-stream")


@router.head("/blobs/{hash_str:path}")
async def head_blob(hash_str: str) -> Response:
    """200 if exists with Content-Length; 404 if not found."""
    if not CAS_ROOT:
        return Response(status_code=503, content="CAS not configured (ZENO_CAS_ROOT)")
    _validate_hash(hash_str)
    backend = _get_backend()
    if not backend.exists(hash_str):
        return Response(status_code=404)
    size = backend.get_size(hash_str)
    return Response(status_code=200, headers={"Content-Length": str(size)})
