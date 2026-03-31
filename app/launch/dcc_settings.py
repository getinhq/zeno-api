"""Resolve DCC executable path from Mongo global settings (extra.dcc_applications)."""
from __future__ import annotations

from typing import Any, Optional

import app.config as app_config
from app.settings.store import get_global_settings


def resolve_dcc_executable_path(dcc: str, dcc_label: Optional[str]) -> Optional[str]:
    """
    Look up path from settings extra.dcc_applications:
    [{ "label": "Blender 4.2", "path": "/...", "dcc_kind": "blender", "default": true }, ...]
    """
    env = app_config.APP_ENV if app_config.APP_ENV in ("production", "staging", "development") else "development"
    try:
        doc = get_global_settings(env)
    except Exception:
        return None

    apps = (doc.get("extra") or {}).get("dcc_applications")
    if not isinstance(apps, list) or not apps:
        return None

    dcc_l = (dcc or "").strip().lower()
    label_wanted = (dcc_label or "").strip()

    def path_for(entry: dict[str, Any]) -> str:
        return str(entry.get("path") or "").strip()

    if label_wanted:
        for app in apps:
            if not isinstance(app, dict):
                continue
            kind = str(app.get("dcc_kind") or app.get("dcc") or "").strip().lower()
            lab = str(app.get("label") or "").strip()
            if kind == dcc_l and lab == label_wanted:
                p = path_for(app)
                return p or None

    for app in apps:
        if not isinstance(app, dict):
            continue
        kind = str(app.get("dcc_kind") or app.get("dcc") or "").strip().lower()
        if kind != dcc_l:
            continue
        if app.get("default") is True:
            p = path_for(app)
            if p:
                return p

    for app in apps:
        if not isinstance(app, dict):
            continue
        kind = str(app.get("dcc_kind") or app.get("dcc") or "").strip().lower()
        if kind == dcc_l:
            p = path_for(app)
            if p:
                return p

    return None
