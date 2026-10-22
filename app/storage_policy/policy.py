"""Studio storage policy: stored on ``organizations.storage_policy`` (JSONB).

Stored shape (everything optional; missing keys fall back to the catalog's recommendations)::

    {"chunking_enabled": true,
     "default_scene_strategy": "chunked",
     "rules": {"blend": "chunked", "fbx": "whole", "exr": "off", "hda": "whole"},
     "updated_at": "...", "updated_by": "..."}

``chunking_enabled = false`` is the master switch: every scene file is stored whole and image
sequences are not accepted, whatever the per-extension rules say (they are kept for later).
"""
from __future__ import annotations

import re
from typing import Any, Optional

from fastapi import HTTPException

from app.storage_policy.catalog import ALGORITHM_VERSION, CATALOG, REVIEW_IMAGE, REVIEW_MOVIE, SCENE, STRATEGIES

_EXT_RE = re.compile(r"^[a-z0-9]{1,12}$")


def normalize_ext(value: str) -> str:
    ext = str(value or "").strip().lower().lstrip(".")
    if "." in ext or "/" in ext:
        ext = ext.rsplit(".", 1)[-1]
    return ext


def kind_of(ext: str) -> str:
    entry = CATALOG.get(ext)
    return entry["kind"] if entry else SCENE


def validate(body: dict) -> dict:
    """Clean a policy update; raises 400 with a readable message."""
    out: dict[str, Any] = {}
    if "chunking_enabled" in body:
        if not isinstance(body["chunking_enabled"], bool):
            raise HTTPException(status_code=400, detail="chunking_enabled must be true or false")
        out["chunking_enabled"] = body["chunking_enabled"]
    if "default_scene_strategy" in body:
        if body["default_scene_strategy"] not in STRATEGIES[SCENE]:
            raise HTTPException(status_code=400, detail="default_scene_strategy must be 'chunked' or 'whole'")
        out["default_scene_strategy"] = body["default_scene_strategy"]
    if "rules" in body:
        rules = body["rules"]
        if not isinstance(rules, dict) or len(rules) > 200:
            raise HTTPException(status_code=400, detail="rules must be an object of extension -> strategy")
        clean: dict[str, str] = {}
        for raw_ext, strategy in rules.items():
            ext = normalize_ext(raw_ext)
            if not _EXT_RE.match(ext):
                raise HTTPException(status_code=400, detail=f"'{raw_ext}' is not a file extension")
            allowed = STRATEGIES[kind_of(ext)]
            if strategy not in allowed:
                raise HTTPException(status_code=400, detail=f".{ext} supports: {', '.join(allowed)}")
            clean[ext] = strategy
        out["rules"] = clean
    return out


def resolve(stored: Optional[dict], ext: str) -> dict:
    """Effective strategy for one extension, with the reason (shown to artists and developers)."""
    stored = stored or {}
    ext = normalize_ext(ext)
    kind = kind_of(ext)
    entry = CATALOG.get(ext)
    enabled = stored.get("chunking_enabled", True)
    rules = stored.get("rules") or {}
    if kind == REVIEW_MOVIE:
        return {"extension": ext, "kind": kind, "strategy": "whole", "source": "fixed",
                "reason": "Movies are stored as one file; chunking compressed video saves nothing."}
    if not enabled:
        strategy = "whole" if kind == SCENE else "off"
        return {"extension": ext, "kind": kind, "strategy": strategy, "source": "master_switch",
                "reason": "Chunking & dedup is turned off for this studio."}
    if ext in rules:
        return {"extension": ext, "kind": kind, "strategy": rules[ext], "source": "rule",
                "reason": f"Studio rule for .{ext}."}
    if entry:
        return {"extension": ext, "kind": kind, "strategy": entry["recommended"], "source": "recommended",
                "reason": f"Recommended for .{ext} ({entry['status']})."}
    default = stored.get("default_scene_strategy", "chunked")
    return {"extension": ext, "kind": kind, "strategy": default, "source": "default",
            "reason": "Default for file types without a rule."}


def describe(stored: Optional[dict]) -> dict:
    """Full policy for clients: switches, every known/custom extension resolved, the catalog."""
    stored = stored or {}
    exts = list(CATALOG) + sorted(e for e in (stored.get("rules") or {}) if e not in CATALOG)
    return {
        "algorithm_version": ALGORITHM_VERSION,
        "chunking_enabled": stored.get("chunking_enabled", True),
        "default_scene_strategy": stored.get("default_scene_strategy", "chunked"),
        "rules": stored.get("rules") or {},
        "effective": {e: resolve(stored, e) for e in exts},
        "catalog": list(CATALOG.values()),
        "updated_at": stored.get("updated_at"),
        "updated_by": stored.get("updated_by"),
    }
