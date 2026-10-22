"""Storage integrity check: every blob the database points at must exist in CAS.

What is checked (per studio, or for the whole server):

- published versions: the delivered file or chunk list (``content_id``), the optional dedup
  chunk list (``metadata.dedup_artifact``), and everything those chunk lists reference
  (v1/v2 chunks; v3 raw chunks, patch/dictionary blobs and the parent chunk lists they
  patch against, followed recursively);
- review items: original upload (or sequence frame list), proxy, thumbnail, filmstrip, frames;
- issue attachments.

Each problem is classified:

- ``broken``   the thing cannot be opened (its file, or a chunk of it, is gone);
- ``degraded`` it still opens, but a derived or optional blob is gone (dedup chunk list,
  review preview). Degraded review previews can be rebuilt from the original.

``find_orphans`` lists stored blobs nothing references (server-wide; blobs are shared across
studios), which is what a future garbage collector would remove.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional
from uuid import UUID

from app.cas.factory import get_cas_backend
from app.cas.paths import is_valid_hash
from app.db import acquire

MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MANIFEST_SCHEMAS = ("chimera.manifest.v1", "chimera.manifest.v2", "chimera.manifest.v3")
CONCURRENCY = 16


@dataclass
class Entity:
    kind: str  # version | review | attachment
    id: str
    label: str
    project: str
    created_at: Optional[str]
    # role -> hash; a blob missing for a "required" role makes the entity broken.
    refs: list[tuple[str, str, bool]] = field(default_factory=list)  # (role, hash, required)
    reprocessable: bool = False


def _json(value) -> Any:
    if value is None or isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


async def _collect(conn, org_id: Optional[UUID]) -> list[Entity]:
    where, params = ("WHERE p.organization_id = $1", [org_id]) if org_id else ("", [])
    entities: list[Entity] = []
    rows = await conn.fetch(
        f"""
        SELECT v.id, v.content_id, v.metadata, v.filename, v.version_number, v.representation, v.created_at,
               a.code AS asset, p.name AS project
        FROM versions v JOIN assets a ON a.id = v.asset_id JOIN projects p ON p.id = a.project_id {where}
        """,
        *params,
    )
    for r in rows:
        e = Entity("version", str(r["id"]), f"{r['asset']} v{r['version_number']:03d} ({r['filename']})",
                   r["project"], r["created_at"].isoformat() if r["created_at"] else None)
        e.refs.append(("published file", r["content_id"], True))
        dedup = ((_json(r["metadata"]) or {}).get("dedup_artifact") or {}).get("content_id")
        if dedup:
            e.refs.append(("dedup chunk list", str(dedup), False))
        entities.append(e)

    rows = await conn.fetch(
        f"""
        SELECT ri.id, ri.revision, ri.kind, ri.source_content_id, ri.proxy_content_id, ri.thumb_content_id,
               ri.filmstrip_content_id, ri.frames, ri.processing_status, ri.created_at,
               t.title AS task, p.name AS project
        FROM review_items ri JOIN tasks t ON t.id = ri.task_id JOIN projects p ON p.id = ri.project_id {where}
        """,
        *params,
    )
    for r in rows:
        e = Entity("review", str(r["id"]), f"{r['task'] or 'Review'} v{r['revision']:03d}", r["project"],
                   r["created_at"].isoformat() if r["created_at"] else None)
        e.refs.append(("review original" if r["kind"] != "sequence" else "sequence frame list", r["source_content_id"], True))
        for f in _json(r["frames"]) or []:
            e.refs.append((f"frame {f.get('frame')}", str(f.get("content_id")), True))
        if r["processing_status"] == "ready":
            for role, col in (("proxy", "proxy_content_id"), ("thumbnail", "thumb_content_id"), ("filmstrip", "filmstrip_content_id")):
                if r[col]:
                    e.refs.append((role, r[col], False))
        e.reprocessable = True
        entities.append(e)

    rows = await conn.fetch(
        f"""
        SELECT ia.id, ia.content_id, ia.filename, ia.created_at, i.title, p.name AS project
        FROM issue_attachments ia JOIN issues i ON i.id = ia.issue_id JOIN projects p ON p.id = i.project_id {where}
        """,
        *params,
    )
    for r in rows:
        e = Entity("attachment", str(r["id"]), f"{r['filename']} on “{r['title']}”", r["project"],
                   r["created_at"].isoformat() if r["created_at"] else None)
        e.refs.append(("attachment", r["content_id"], True))
        entities.append(e)
    return entities


def _manifest_deps(data: dict) -> list[tuple[str, str]]:
    """(role, hash) of everything a chunk list needs."""
    deps: list[tuple[str, str]] = []
    for c in data.get("chunks") or []:
        if isinstance(c, dict) and c.get("hash"):
            deps.append(("chunk", str(c["hash"]).lower()))
    for s in data.get("segments") or []:
        if not isinstance(s, dict):
            continue
        if s.get("kind") == "raw_chunk" and s.get("hash"):
            deps.append(("chunk", str(s["hash"]).lower()))
        elif s.get("kind") == "zstd_dict_patch":
            for role, key in (("patch", "patch_hash"), ("patch dictionary", "dict_hash"), ("parent chunk list", "parent_content_id")):
                if s.get(key):
                    deps.append((role, str(s[key]).lower()))
    return deps


class _Store:
    """Memoised, concurrent exists/manifest lookups against the CAS backend."""

    def __init__(self) -> None:
        self.backend = get_cas_backend()
        self.exists: dict[str, bool] = {}
        self.manifests: dict[str, Optional[dict]] = {}
        self.sem = asyncio.Semaphore(CONCURRENCY)

    async def check(self, hashes: set[str]) -> None:
        todo = [h for h in hashes if h not in self.exists]

        async def one(h: str) -> None:
            async with self.sem:
                self.exists[h] = is_valid_hash(h) and await asyncio.to_thread(self.backend.exists, h)

        await asyncio.gather(*(one(h) for h in todo))

    async def manifest(self, h: str) -> Optional[dict]:
        if h in self.manifests:
            return self.manifests[h]
        data = None
        if self.exists.get(h):
            async with self.sem:
                try:
                    size = await asyncio.to_thread(self.backend.get_size, h)
                    if size <= MAX_MANIFEST_BYTES:
                        raw = await asyncio.to_thread(lambda: b"".join(self.backend.get_stream(h)))
                        parsed = json.loads(raw)
                        if isinstance(parsed, dict) and parsed.get("schema") in MANIFEST_SCHEMAS:
                            data = parsed
                except Exception:  # noqa: BLE001 - not JSON: a plain file
                    data = None
        self.manifests[h] = data
        return data


async def _expand(store: _Store, entity: Entity) -> list[tuple[str, str, bool]]:
    """The entity's refs plus everything their chunk lists need (recursively)."""
    out = list(entity.refs)
    queue = [(h, required) for role, h, required in entity.refs if role in ("published file", "dedup chunk list")]
    seen: set[str] = set()
    while queue:
        h, required = queue.pop()
        if h in seen:
            continue
        seen.add(h)
        await store.check({h})
        data = await store.manifest(h)
        if not data:
            continue
        deps = _manifest_deps(data)
        await store.check({d for _, d in deps})
        for role, d in deps:
            out.append((role, d, required))
            if role == "parent chunk list":
                queue.append((d, required))
    return out


def _fix(entity: Entity, missing: list[dict], broken: bool) -> str:
    if entity.kind == "version":
        if broken:
            return "Republish this version from the original work file in the DCC."
        return "Opens fine (the full file is stored). The next publish starts a fresh dedup chain."
    if entity.kind == "review":
        if broken:
            return "The original upload is gone: submit the playblast/render again."
        return "Rebuild previews from the original."
    return "Attach the file to the issue again."


async def run_check(org_id: Optional[UUID]) -> dict:
    started = time.time()
    store = _Store()
    async with acquire() as conn:
        entities = await _collect(conn, org_id)
    await store.check({h for e in entities for _, h, _ in e.refs if h})
    problems = []
    blob_refs = 0
    for e in entities:
        refs = await _expand(store, e)
        blob_refs += len(refs)
        missing = [{"role": role, "hash": h} for role, h, _ in refs if not store.exists.get(h)]
        if not missing:
            continue
        broken = any(not store.exists.get(h) for _, h, required in refs if required)
        problems.append({
            "kind": e.kind,
            "id": e.id,
            "label": e.label,
            "project": e.project,
            "created_at": e.created_at,
            "severity": "broken" if broken else "degraded",
            "missing": missing[:50],
            "missing_count": len(missing),
            "fix": _fix(e, missing, broken),
            "can_rebuild": e.kind == "review" and not broken and e.reprocessable,
        })
    problems.sort(key=lambda p: (p["severity"] != "broken", p["project"], p["label"]))
    counts = {k: sum(1 for e in entities if e.kind == k) for k in ("version", "review", "attachment")}
    return {
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "duration_seconds": round(time.time() - started, 2),
        "summary": {
            "versions": counts["version"],
            "reviews": counts["review"],
            "attachments": counts["attachment"],
            "blobs_checked": len(store.exists),
            "blob_references": blob_refs,
            "missing_blobs": sum(1 for v in store.exists.values() if not v),
            "broken": sum(1 for p in problems if p["severity"] == "broken"),
            "degraded": sum(1 for p in problems if p["severity"] == "degraded"),
        },
        "ok": not problems,
        "problems": problems,
    }


async def referenced_blobs() -> set[str]:
    """Every hash the database references, chunk lists expanded (server-wide)."""
    store = _Store()
    async with acquire() as conn:
        entities = await _collect(conn, None)
    await store.check({h for e in entities for _, h, _ in e.refs if h})
    refs: set[str] = set()
    for e in entities:
        refs.update(h for _, h, _ in await _expand(store, e))
    return refs


async def find_orphans() -> dict:
    """Stored blobs nothing references. Shared across studios, so server-wide only."""
    refs = await referenced_blobs()
    backend = get_cas_backend()
    stored = await asyncio.to_thread(lambda: list(backend.list_blobs()))
    orphans = [(h, s) for h, s in stored if h not in refs]
    return {
        "stored_blobs": len(stored),
        "stored_bytes": sum(s for _, s in stored),
        "orphan_blobs": len(orphans),
        "orphan_bytes": sum(s for _, s in orphans),
        "orphans": [h for h, _ in orphans],
    }
