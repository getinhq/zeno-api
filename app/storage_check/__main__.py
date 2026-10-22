"""Command line: ``python -m app.storage_check [--studio SLUG] [--orphans] [--json]``.

Checks every studio (or one) and exits 1 when something is broken, so it can run from cron/CI.
``--orphans`` also lists blobs nothing references (server-wide, read-only: nothing is deleted).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.db import acquire, close_pool
from app.storage_check.service import find_orphans, run_check


def _mb(n: int) -> str:
    return f"{n / 1e6:.1f} MB"


async def main() -> int:
    parser = argparse.ArgumentParser(description="Check that every stored file Zeno references exists.")
    parser.add_argument("--studio", help="studio slug (default: all studios)")
    parser.add_argument("--orphans", action="store_true", help="also report unreferenced blobs")
    parser.add_argument("--json", action="store_true", help="print the full JSON report")
    args = parser.parse_args()

    async with acquire() as conn:
        orgs = await conn.fetch(
            "SELECT id, slug, name FROM organizations WHERE $1::text IS NULL OR slug = $1 ORDER BY created_at",
            args.studio,
        )
    if not orgs:
        print(f"No studio {args.studio!r}", file=sys.stderr)
        return 2
    out: dict = {"studios": {}}
    broken = 0
    for o in orgs:
        report = await run_check(o["id"])
        out["studios"][o["slug"]] = report
        s = report["summary"]
        broken += s["broken"]
        if not args.json:
            status = "OK" if report["ok"] else f"{s['broken']} broken, {s['degraded']} degraded"
            print(f"{o['name']} ({o['slug']}): {s['versions']} versions, {s['reviews']} reviews, "
                  f"{s['attachments']} attachments, {s['blobs_checked']} blobs -> {status}")
            for p in report["problems"]:
                roles = ", ".join(sorted({m['role'] for m in p['missing']}))
                print(f"   [{p['severity']}] {p['kind']} {p['label']} ({p['project']}): missing {p['missing_count']} ({roles}). {p['fix']}")
    if args.orphans:
        orphans = await find_orphans()
        out["orphans"] = orphans
        if not args.json:
            print(f"Storage: {orphans['stored_blobs']} blobs, {_mb(orphans['stored_bytes'])}; unreferenced: "
                  f"{orphans['orphan_blobs']} blobs, {_mb(orphans['orphan_bytes'])} (not deleted)")
    if args.json:
        print(json.dumps(out, indent=2))
    await close_pool()
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
