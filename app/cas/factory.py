"""Select NAS or S3 CAS backend from configuration and settings overrides."""
from __future__ import annotations

from typing import NamedTuple, Union

import app.config as cfg
from app.cas.backend import NASBackend
from app.cas.s3_backend import S3Backend
from app.settings.store import get_global_settings

# Union type for type checkers / callers
CasBackend = Union[NASBackend, S3Backend]


class CasSelection(NamedTuple):
    mode: str
    local_root: str | None


def _s3_configured() -> bool:
    return bool(cfg.S3_ENDPOINT_URL and cfg.S3_ACCESS_KEY and cfg.S3_SECRET_KEY)


def _resolve_cas_selection() -> CasSelection:
    """
    Resolve CAS storage mode from settings first, then environment fallback.

    Settings contract (global settings extra.cas):
    - use_minio: bool
      - true -> force S3/MinIO
      - false -> force NAS/local CAS
    - local_cas_root: str (optional path hint for NAS mode)
    """
    try:
        g = get_global_settings(cfg.APP_ENV)
    except Exception:
        g = {}
    extra = g.get("extra") if isinstance(g, dict) else {}
    cas = extra.get("cas") if isinstance(extra, dict) else {}

    use_minio = cas.get("use_minio") if isinstance(cas, dict) else None
    local_cas_root = cas.get("local_cas_root") if isinstance(cas, dict) else None
    local_root = str(local_cas_root).strip() if isinstance(local_cas_root, str) and local_cas_root.strip() else cfg.CAS_ROOT

    if isinstance(use_minio, bool):
        return CasSelection(mode=("s3" if use_minio else "nas"), local_root=local_root)
    return CasSelection(mode=cfg.CAS_STORAGE_BACKEND, local_root=local_root)


def is_cas_configured() -> bool:
    """True if either S3 (when mode allows) or NAS root is available."""
    sel = _resolve_cas_selection()
    mode = sel.mode
    if mode == "s3":
        return _s3_configured()
    if mode == "nas":
        return bool(sel.local_root)
    # auto
    return _s3_configured() or bool(sel.local_root)


def get_cas_backend() -> CasBackend:
    """
    Return the active CAS backend.

    - CAS_STORAGE_BACKEND=s3 → S3 only (raises if misconfigured).
    - CAS_STORAGE_BACKEND=nas → filesystem only.
    - CAS_STORAGE_BACKEND=auto → S3 if endpoint + keys set, else NAS if CAS_ROOT set.
    """
    sel = _resolve_cas_selection()
    mode = sel.mode
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
        if not sel.local_root:
            raise RuntimeError("CAS NAS not configured (ZENO_CAS_ROOT / CAS_ROOT)")
        return NASBackend(sel.local_root)
    if mode == "auto":
        if _s3_configured():
            return S3Backend(
                cfg.S3_ENDPOINT_URL or "",
                cfg.S3_BUCKET_CAS,
                cfg.S3_ACCESS_KEY or "",
                cfg.S3_SECRET_KEY or "",
            )
        if sel.local_root:
            return NASBackend(sel.local_root)
        raise RuntimeError("CAS not configured (set S3_* for MinIO or ZENO_CAS_ROOT for filesystem)")
    raise RuntimeError(f"Invalid CAS_STORAGE_BACKEND: {mode!r}")
