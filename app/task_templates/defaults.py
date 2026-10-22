"""Built-in task templates every studio starts with (editable per studio)."""
from __future__ import annotations

TASK_TYPES = (
    "concept", "storyboard", "model", "texture", "rig", "lookdev", "groom", "cfx",
    "layout", "animation", "fx", "lighting", "render", "comp", "edit",
)

ASSET_TYPES = (
    "character", "prop", "environment", "fx", "rig", "texture_set", "groom", "shader",
)


def _chain(*steps: tuple[str, str, int]) -> list[dict]:
    """Linear pipeline: each step waits on the previous one."""
    return [
        {"type": t, "title": title, "duration_days": days, "depends_on": [i - 1] if i else []}
        for i, (t, title, days) in enumerate(steps)
    ]


DEFAULT_TEMPLATES: list[dict] = [
    {
        "name": "Character",
        "entity_kind": "asset",
        "asset_type": "character",
        "steps": _chain(
            ("concept", "Concept", 3),
            ("model", "Model", 5),
            ("texture", "Texture", 3),
            ("rig", "Rig", 5),
            ("lookdev", "Lookdev", 2),
        ),
    },
    {
        "name": "Prop",
        "entity_kind": "asset",
        "asset_type": "prop",
        "steps": _chain(("model", "Model", 2), ("texture", "Texture", 1), ("lookdev", "Lookdev", 1)),
    },
    {
        "name": "Environment",
        "entity_kind": "asset",
        "asset_type": "environment",
        "steps": _chain(
            ("concept", "Concept", 3),
            ("model", "Model", 6),
            ("texture", "Texture", 4),
            ("lookdev", "Lookdev", 3),
        ),
    },
    {
        "name": "FX element",
        "entity_kind": "asset",
        "asset_type": "fx",
        "steps": _chain(("fx", "FX setup", 4)),
    },
    {
        "name": "Groom",
        "entity_kind": "asset",
        "asset_type": "groom",
        "steps": _chain(("groom", "Groom", 4), ("cfx", "Hair sim setup", 2)),
    },
    {
        "name": "Shot",
        "entity_kind": "shot",
        "asset_type": None,
        "steps": _chain(
            ("layout", "Layout", 2),
            ("animation", "Animation", 5),
            ("lighting", "Lighting", 3),
            ("render", "Render", 1),
            ("comp", "Comp", 2),
        ),
    },
]
