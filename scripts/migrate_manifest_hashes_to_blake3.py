#!/usr/bin/env python3
"""
Migrate legacy manifest content_ids to BLAKE3 content IDs.

This utility:
- scans versions.content_id
- detects manifest JSON blobs in CAS
- computes BLAKE3(manifest_bytes)
- stores manifest under the new BLAKE3 key (idempotent)
- updates versions.content_id and metadata migration marker

It intentionally does not rewrite chunk IDs inside legacy manifests.
"""
from __future__ import annotations

import asyncio
import json
from io import BytesIO

from blake3 import blake3

from app.cas.factory import get_cas_backend
from app.db import acquire


async def main() -> None:
    backend = get_cas_backend()
    migrated = 0
    inspected = 0

    async with acquire() as conn:
        rows = await conn.fetch("SELECT id, content_id, metadata FROM versions ORDER BY created_at ASC")
        for row in rows:
            inspected += 1
            version_id = row["id"]
            old_hash = str(row["content_id"]).strip().lower()
            if not backend.exists(old_hash):
                continue

            body = b"".join(backend.get_stream(old_hash))
            try:
                doc = json.loads(body.decode("utf-8"))
            except Exception:
                continue
            if not isinstance(doc, dict):
                continue
            if str(doc.get("schema") or "").strip().lower() != "chimera.manifest.v1":
                continue

            new_hash = blake3(body).hexdigest()
            if new_hash == old_hash:
                continue
            if not backend.exists(new_hash):
                backend.put_stream(new_hash, BytesIO(body))

            prev_meta = row["metadata"] or {}
            if not isinstance(prev_meta, dict):
                prev_meta = {}
            prev_meta["hash_algo"] = "blake3"
            prev_meta["migrated_from_hash"] = old_hash
            prev_meta["migration"] = "manifest_content_id_to_blake3"

            await conn.execute(
                """
                UPDATE versions
                SET content_id = $1, metadata = $2::jsonb
                WHERE id = $3
                """,
                new_hash,
                json.dumps(prev_meta),
                version_id,
            )
            migrated += 1

    print(f"Inspected versions: {inspected}")
    print(f"Migrated manifests: {migrated}")


if __name__ == "__main__":
    asyncio.run(main())

