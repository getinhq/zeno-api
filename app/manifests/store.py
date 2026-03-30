"""MongoDB store for Chimera manifest documents keyed by manifest_hash.

Chunk payloads stay in CAS (filesystem or S3); only the manifest JSON lives here.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from app.settings.store import get_mongo_db

COLLECTION = "chimera_manifests"
DEFAULT_SCHEMA = "chimera.manifest.v2"


def ensure_manifest_indexes() -> None:
    """Unique index on manifest_hash. Safe to call at API startup."""
    db = get_mongo_db()
    db[COLLECTION].create_index("manifest_hash", unique=True)


def get_manifest_document(manifest_hash: str) -> Optional[Dict[str, Any]]:
    """Return the stored manifest JSON object, or None if missing."""
    h = manifest_hash.strip().lower()
    doc = get_mongo_db()[COLLECTION].find_one({"manifest_hash": h})
    if not doc:
        return None
    inner = doc.get("document")
    if isinstance(inner, dict):
        return inner
    return None


def upsert_manifest(
    manifest_hash: str,
    document: Dict[str, Any],
    *,
    schema: str = DEFAULT_SCHEMA,
) -> None:
    """Insert or replace a manifest by hash."""
    h = manifest_hash.strip().lower()
    now = datetime.utcnow()
    get_mongo_db()[COLLECTION].update_one(
        {"manifest_hash": h},
        {
            "$set": {
                "manifest_hash": h,
                "schema": schema,
                "document": document,
                "updated_at": now,
            }
        },
        upsert=True,
    )
