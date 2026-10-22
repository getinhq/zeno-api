"""Built-in project templates for small studios.

``structure`` defaults can be overridden per project in the wizard. Milestones are placed
at a fraction of the project's duration (``at`` = 0..1 between start and end date).
"""
from __future__ import annotations

PROJECT_TEMPLATES: list[dict] = [
    {
        "id": "short_film",
        "name": "Short film",
        "description": "A single film split into sequences and shots. Typical indie or student short.",
        "default_duration_days": 120,
        "fps": 24,
        "resolution": "1920x1080",
        "structure": {"episodes": 1, "sequences_per_episode": 4, "shots_per_sequence": 6, "frames_per_shot": 96},
        "episode_label": "Film",
        "asset_prompts": ["character", "prop", "environment"],
        "milestones": [
            {"name": "Script & storyboard locked", "at": 0.1, "kind": "review"},
            {"name": "Animatic approved", "at": 0.25, "kind": "review"},
            {"name": "Assets finished", "at": 0.45, "kind": "milestone"},
            {"name": "Animation locked", "at": 0.75, "kind": "review"},
            {"name": "Final delivery", "at": 1.0, "kind": "delivery"},
        ],
    },
    {
        "id": "series",
        "name": "Series / web series",
        "description": "Several episodes sharing characters and environments, e.g. a YouTube animated series.",
        "default_duration_days": 180,
        "fps": 24,
        "resolution": "1920x1080",
        "structure": {"episodes": 3, "sequences_per_episode": 3, "shots_per_sequence": 5, "frames_per_shot": 72},
        "episode_label": "Episode",
        "asset_prompts": ["character", "prop", "environment"],
        "milestones": [
            {"name": "Shared assets ready", "at": 0.25, "kind": "milestone"},
            {"name": "Episode 1 delivered", "at": 0.5, "kind": "delivery"},
            {"name": "Mid-season review", "at": 0.7, "kind": "review"},
            {"name": "Season delivered", "at": 1.0, "kind": "delivery"},
        ],
    },
    {
        "id": "commercial",
        "name": "Commercial / ad",
        "description": "A short client spot with client approvals at each stage.",
        "default_duration_days": 35,
        "fps": 25,
        "resolution": "1920x1080",
        "structure": {"episodes": 1, "sequences_per_episode": 1, "shots_per_sequence": 8, "frames_per_shot": 75},
        "episode_label": "Spot",
        "asset_prompts": ["character", "prop"],
        "milestones": [
            {"name": "Storyboard approval (client)", "at": 0.15, "kind": "review"},
            {"name": "Animatic approval (client)", "at": 0.35, "kind": "review"},
            {"name": "First full pass", "at": 0.7, "kind": "review"},
            {"name": "Final delivery", "at": 1.0, "kind": "delivery"},
        ],
    },
    {
        "id": "music_video",
        "name": "Music video",
        "description": "Sequences follow the song structure (intro, verses, choruses).",
        "default_duration_days": 50,
        "fps": 24,
        "resolution": "1920x1080",
        "structure": {"episodes": 1, "sequences_per_episode": 5, "shots_per_sequence": 4, "frames_per_shot": 48},
        "sequence_names": ["Intro", "Verse 1", "Chorus 1", "Verse 2", "Chorus 2", "Bridge", "Outro"],
        "episode_label": "Video",
        "asset_prompts": ["character", "environment"],
        "milestones": [
            {"name": "Treatment approved", "at": 0.1, "kind": "review"},
            {"name": "Animatic cut to track", "at": 0.3, "kind": "review"},
            {"name": "Rough cut", "at": 0.7, "kind": "review"},
            {"name": "Release master", "at": 1.0, "kind": "delivery"},
        ],
    },
    {
        "id": "game_cinematic",
        "name": "Game cinematic / trailer",
        "description": "Cinematic sequences for a game trailer or in-game cutscenes.",
        "default_duration_days": 75,
        "fps": 30,
        "resolution": "3840x2160",
        "structure": {"episodes": 1, "sequences_per_episode": 3, "shots_per_sequence": 6, "frames_per_shot": 90},
        "episode_label": "Cinematic",
        "asset_prompts": ["character", "prop", "environment", "fx"],
        "milestones": [
            {"name": "Previs approved", "at": 0.2, "kind": "review"},
            {"name": "Assets in engine / scene", "at": 0.45, "kind": "milestone"},
            {"name": "Animation lock", "at": 0.75, "kind": "review"},
            {"name": "Trailer delivered", "at": 1.0, "kind": "delivery"},
        ],
    },
    {
        "id": "blank",
        "name": "Blank project",
        "description": "Just the project. Add episodes, sequences and shots yourself.",
        "default_duration_days": 90,
        "fps": 24,
        "resolution": "1920x1080",
        "structure": {"episodes": 0, "sequences_per_episode": 0, "shots_per_sequence": 0, "frames_per_shot": 96},
        "episode_label": "Episode",
        "asset_prompts": [],
        "milestones": [],
    },
]

TEMPLATES_BY_ID = {t["id"]: t for t in PROJECT_TEMPLATES}

LIMITS = {"episodes": 50, "sequences_per_episode": 50, "shots_per_sequence": 200, "frames_per_shot": 100000}
MAX_TOTAL_SHOTS = 500
MAX_STARTER_ASSETS = 200
