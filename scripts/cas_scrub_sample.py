#!/usr/bin/env python3
"""
Sample CAS scrubber for Chimera reliability checks.

The scrubber randomly samples version content IDs, verifies blob readability, and
records simple pass/fail counters. It is designed as a starter utility for the
Integrity + Self-healing reliability pillars.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
from blake3 import blake3

from app.cas.factory import get_cas_backend
from app.db import acquire


def _parse_manifest_chunks(manifest_blob: bytes) -> list[str]:
    try:
        doc = json.loads(manifest_blob.decode("utf-8"))
    except Exception:
        return []
    if not isinstance(doc, dict):
        return []
    schema = str(doc.get("schema") or "").strip().lower()
    if schema in ("chimera.manifest.v1", "chimera.manifest.v2"):
        out = []
        for ch in doc.get("chunks") or []:
            if isinstance(ch, dict):
                h = str(ch.get("hash") or "").strip().lower()
                if h:
                    out.append(h)
        return out
    if schema == "chimera.manifest.v3":
        out = []
        for seg in doc.get("segments") or []:
            if not isinstance(seg, dict):
                continue
            kind = str(seg.get("kind") or "").strip().lower()
            if kind == "raw_chunk":
                h = str(seg.get("hash") or "").strip().lower()
                if h:
                    out.append(h)
            elif kind == "zstd_dict_patch":
                dh = str(seg.get("dict_hash") or "").strip().lower()
                ph = str(seg.get("patch_hash") or "").strip().lower()
                if dh:
                    out.append(dh)
                if ph:
                    out.append(ph)
        return out
    return []


async def run(sample_size: int, seed: int) -> None:
    random.seed(seed)
    backend = get_cas_backend()

    async with acquire() as conn:
        rows = await conn.fetch("SELECT DISTINCT content_id FROM versions WHERE content_id IS NOT NULL")
    ids = [str(r["content_id"]).strip().lower() for r in rows if r["content_id"]]
    if not ids:
        print("No version content IDs found.")
        return

    sample = ids if len(ids) <= sample_size else random.sample(ids, sample_size)
    ok = 0
    missing = 0
    corrupted = 0
    verified_children = 0

    for cid in sample:
        try:
            if not backend.exists(cid):
                missing += 1
                continue
            blob = b"".join(backend.get_stream(cid))
            if blake3(blob).hexdigest() != cid:
                corrupted += 1
                continue
            # If this is a manifest, also verify all referenced child blobs exist and hash-match.
            for child in _parse_manifest_chunks(blob):
                if not backend.exists(child):
                    missing += 1
                    continue
                cblob = b"".join(backend.get_stream(child))
                if blake3(cblob).hexdigest() != child:
                    corrupted += 1
                    continue
                verified_children += 1
            ok += 1
        except FileNotFoundError:
            missing += 1
        except Exception:
            corrupted += 1

    print(f"Scrub sample size: {len(sample)}")
    print(f"OK: {ok}")
    print(f"Missing: {missing}")
    print(f"Corrupted/Error: {corrupted}")
    print(f"Verified child blobs: {verified_children}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Sample CAS scrubber")
    parser.add_argument("--sample-size", type=int, default=100)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    asyncio.run(run(sample_size=args.sample_size, seed=args.seed))


if __name__ == "__main__":
    main()

