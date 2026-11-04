"""Compare what a shot file contains with what Zeno expects (pure functions, no I/O)."""
from __future__ import annotations

from typing import Any

# How a linked asset version compares with the asset's approved versions.
#   current   - the newest approved version
#   outdated  - an older version than the newest approved one
#   ahead     - newer than anything approved (work in progress)
#   unapproved- the asset has no approved version yet
#   unknown   - the library is not a Zeno version (linked from elsewhere)
STATES = ("current", "outdated", "ahead", "unapproved", "unknown")


def asset_state(used: int, approved: int | None) -> str:
    if approved is None:
        return "unapproved"
    if used == approved:
        return "current"
    return "outdated" if used < approved else "ahead"


def build_report(
    facts: dict[str, Any],
    *,
    versions_by_cid: dict[str, dict[str, Any]],
    approved_latest: dict[tuple[str, str, str], int],
    casting: list[dict[str, Any]],
) -> dict[str, Any]:
    """``assets`` (one row per linked library), ``not_cast``, ``missing`` and ``counts``."""
    rows: list[dict[str, Any]] = []
    in_file: dict[str, dict[str, Any]] = {}
    for lib in facts.get("assets") or []:
        cid = str(lib.get("content_id") or "").lower()
        v = versions_by_cid.get(cid)
        if v is None:
            rows.append({"library": lib.get("library"), "state": "unknown"})
            continue
        key = (str(v["asset_id"]), str(v["representation"]), str(v["pipeline_stage"] or ""))
        approved = approved_latest.get(key)
        row = {
            "library": lib.get("library"),
            "asset_id": str(v["asset_id"]),
            "code": v["asset_code"],
            "name": v["asset_name"],
            "representation": v["representation"],
            "pipeline_stage": v["pipeline_stage"] or "",
            "version_id": str(v["version_id"]),
            "version": int(v["version_number"]),
            "approved_version": approved,
            "state": asset_state(int(v["version_number"]), approved),
        }
        rows.append(row)
        in_file.setdefault(row["asset_id"], row)
    cast_ids = {str(c["asset_id"]): c for c in casting}
    not_cast = [{"asset_id": a, "code": r["code"], "name": r["name"]} for a, r in in_file.items() if a not in cast_ids]
    missing = [{"asset_id": a, "code": c.get("code"), "name": c.get("name")} for a, c in cast_ids.items() if a not in in_file]
    counts = {s: sum(1 for r in rows if r["state"] == s) for s in STATES}
    return {"assets": rows, "not_cast": not_cast, "missing": missing, "counts": counts}


def clean_facts(body: dict[str, Any]) -> dict[str, Any]:
    """Keep the known fields with sane types; DCCs send what they have."""

    def opt_int(v: Any) -> int | None:
        try:
            return int(v) if v is not None and v != "" else None
        except (TypeError, ValueError):
            return None

    assets = []
    for a in (body.get("assets") or [])[:500]:
        if not isinstance(a, dict):
            continue
        assets.append({
            "content_id": str(a.get("content_id") or "").strip().lower()[:64] or None,
            "library": str(a.get("library") or "")[:500],
        })
    return {
        "dcc": str(body.get("dcc") or "")[:40],
        "file": str(body.get("file") or "")[:300],
        "frame_start": opt_int(body.get("frame_start")),
        "frame_end": opt_int(body.get("frame_end")),
        "fps": str(body.get("fps") or "")[:20],
        "resolution": str(body.get("resolution") or "")[:20],
        "camera": str(body.get("camera") or "")[:120],
        "assets": assets,
    }
