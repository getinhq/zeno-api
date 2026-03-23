"""Select NAS or S3 CAS backend from configuration."""
from __future__ import annotations

from typing import Union

import app.config as cfg
from app.cas.backend import NASBackend
from app.cas.s3_backend import S3Backend

# Union type for type checkers / callers
CasBackend = Union[NASBackend, S3Backend]


def _s3_configured() -> bool:
    return bool(cfg.S3_ENDPOINT_URL and cfg.S3_ACCESS_KEY and cfg.S3_SECRET_KEY)


def is_cas_configured() -> bool:
    """True if either S3 (when mode allows) or NAS root is available."""
    mode = cfg.CAS_STORAGE_BACKEND
    if mode == "s3":
        return _s3_configured()
    if mode == "nas":
        return bool(cfg.CAS_ROOT)
    # auto
    return _s3_configured() or bool(cfg.CAS_ROOT)


def get_cas_backend() -> CasBackend:
    """
    Return the active CAS backend.

    - CAS_STORAGE_BACKEND=s3 → S3 only (raises if misconfigured).
    - CAS_STORAGE_BACKEND=nas → filesystem only.
    - CAS_STORAGE_BACKEND=auto → S3 if endpoint + keys set, else NAS if CAS_ROOT set.
    """
    mode = cfg.CAS_STORAGE_BACKEND
    if mode == "s3":
        if not _s3_configured():
            raise RuntimeError("CAS S3 not configured (S3_ENDPOINT_URL, S3_ACCESS_KEY, S3_SECRET_KEY)")
        return S3Backend(
            cfg.S3_ENDPOINT_URL or "",
            cfg.S3_BUCKET_CAS,
            cfg.S3_ACCESS_KEY or "",
            cfg.S3_SECRET_KEY or "",
        )
    if mode == "nas":
        if not cfg.CAS_ROOT:
            raise RuntimeError("CAS NAS not configured (ZENO_CAS_ROOT / CAS_ROOT)")
        return NASBackend(cfg.CAS_ROOT)
    if mode == "auto":
        if _s3_configured():
            return S3Backend(
                cfg.S3_ENDPOINT_URL or "",
                cfg.S3_BUCKET_CAS,
                cfg.S3_ACCESS_KEY or "",
                cfg.S3_SECRET_KEY or "",
            )
        if cfg.CAS_ROOT:
            return NASBackend(cfg.CAS_ROOT)
        raise RuntimeError("CAS not configured (set S3_* for MinIO or ZENO_CAS_ROOT for filesystem)")
    raise RuntimeError(f"Invalid CAS_STORAGE_BACKEND: {mode!r}")
