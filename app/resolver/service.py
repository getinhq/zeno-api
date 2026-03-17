"""Resolver service: look up content_id, filename, size from Version DB by asset URI parts."""
from __future__ import annotations

from typing import Any, Dict, Optional, Union
from uuid import UUID

from app.db import acquire
from app.resolver.uri_parser import is_uuid


async def resolve(
    project_spec: str,
    asset_spec: str,
    version_spec: Union[str, int],
    representation: str,
) -> Optional[Dict[str, Any]]:
    """Resolve (project, asset, version, representation) to content_id, filename, size.

    project_spec and asset_spec can be code or UUID; version_spec is "latest" or int.
    Returns dict with content_id, filename, size (size may be None), or None if not found.
    """
    async with acquire() as conn:
        # 1. Resolve project_id
        if is_uuid(project_spec):
            try:
                project_id = UUID(project_spec)
            except ValueError:
                return None
            row = await conn.fetchrow("SELECT id FROM projects WHERE id = $1", project_id)
        else:
            row = await conn.fetchrow("SELECT id FROM projects WHERE code = $1", project_spec)
        if not row:
            return None
        project_id = row["id"]

        # 2. Resolve asset_id
        if is_uuid(asset_spec):
            try:
                asset_uuid = UUID(asset_spec)
            except ValueError:
                return None
            row = await conn.fetchrow(
                "SELECT id FROM assets WHERE id = $1 AND project_id = $2",
                asset_uuid,
                project_id,
            )
        else:
            row = await conn.fetchrow(
                "SELECT id FROM assets WHERE code = $1 AND project_id = $2",
                asset_spec,
                project_id,
            )
        if not row:
            return None
        asset_id = row["id"]

        # 3. Resolve version row
        if version_spec == "latest":
            row = await conn.fetchrow(
                """SELECT content_id, filename, size_bytes
                   FROM versions
                   WHERE asset_id = $1 AND representation = $2
                   ORDER BY version_number DESC
                   LIMIT 1""",
                asset_id,
                representation,
            )
        else:
            row = await conn.fetchrow(
                """SELECT content_id, filename, size_bytes
                   FROM versions
                   WHERE asset_id = $1 AND representation = $2 AND version_number = $3""",
                asset_id,
                representation,
                version_spec,
            )
        if not row:
            return None

        return {
            "content_id": row["content_id"],
            "filename": row["filename"],
            "size": row["size_bytes"],
        }
