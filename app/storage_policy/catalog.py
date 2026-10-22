"""What the pipeline knows how to store, per file extension, and how mature each path is.

This catalog is the developer-facing contract: each entry says which storage strategies an
extension supports, which one we recommend, and the status of its dedup algorithm. Bump
``ALGORITHM_VERSION`` (and an entry's ``status``/``notes``) when an algorithm improves, so
studios can see what changed and opt in.

Strategies
  chunked    content-defined chunking (FastCDC); with a DCC canonicalizer when one exists, so
             volatile bytes (timestamps, pointers) do not defeat dedup. Only changed chunks
             are uploaded and stored.
  whole      the file is one blob. Identical files are still stored once; any change stores
             the whole file again.
  per_frame  review image sequences: each frame is one blob, so unchanged frames are stored once.
  off        review image sequences are not accepted for this format (single images still are).
"""
from __future__ import annotations

ALGORITHM_VERSION = "2026.10"

SCENE = "scene"
REVIEW_IMAGE = "review_image"
REVIEW_MOVIE = "review_movie"

STRATEGIES = {
    SCENE: ("chunked", "whole"),
    REVIEW_IMAGE: ("per_frame", "off"),
    REVIEW_MOVIE: ("whole",),
}

# ext: (kind, label, dcc, canonicalizer, status, recommended, notes)
_ENTRIES = [
    ("blend", SCENE, "Blender scene", "blender", True, "beta", "chunked",
     "Canonicalizer strips pointer/timestamp noise before chunking. Known gap: identical geometry with different memory pointers is not yet byte-identical."),
    ("ma", SCENE, "Maya ASCII", "maya", True, "beta", "chunked", "Canonicalizer normalizes float precision and node order."),
    ("mb", SCENE, "Maya binary", "maya", False, "experimental", "chunked", "Raw chunking only; no canonicalizer yet."),
    ("usd", SCENE, "OpenUSD", "usd", True, "beta", "chunked", "Canonicalizer for layer metadata."),
    ("usda", SCENE, "OpenUSD ASCII", "usd", True, "beta", "chunked", "Canonicalizer for layer metadata."),
    ("usdc", SCENE, "OpenUSD crate", "usd", True, "beta", "chunked", "Canonicalizer for layer metadata."),
    ("usdz", SCENE, "OpenUSD package", "usd", True, "experimental", "chunked", "Zip container; chunking helps little."),
    ("abc", SCENE, "Alembic cache", None, False, "experimental", "chunked", "Raw chunking; good for caches that change on some frames."),
    ("fbx", SCENE, "FBX", None, False, "experimental", "chunked", "Raw chunking only."),
    ("obj", SCENE, "OBJ", None, False, "experimental", "chunked", "Raw chunking only."),
    ("hip", SCENE, "Houdini scene", "houdini", False, "experimental", "chunked", "Raw chunking; canonicalizer planned."),
    ("hipnc", SCENE, "Houdini (non-commercial)", "houdini", False, "experimental", "chunked", "Raw chunking; canonicalizer planned."),
    ("nk", SCENE, "Nuke script", "nuke", False, "experimental", "chunked", "Raw chunking only."),
    ("exr", REVIEW_IMAGE, "OpenEXR frames", None, False, "stable", "per_frame",
     "One blob per frame. Renderers that write the date into EXR headers defeat dedup."),
    ("png", REVIEW_IMAGE, "PNG frames", None, False, "stable", "per_frame", "One blob per frame. Zeno's Blender playblasts strip volatile metadata."),
    ("tif", REVIEW_IMAGE, "TIFF frames", None, False, "stable", "per_frame", "One blob per frame."),
    ("tiff", REVIEW_IMAGE, "TIFF frames", None, False, "stable", "per_frame", "One blob per frame."),
    ("jpg", REVIEW_IMAGE, "JPEG frames", None, False, "stable", "per_frame", "One blob per frame."),
    ("jpeg", REVIEW_IMAGE, "JPEG frames", None, False, "stable", "per_frame", "One blob per frame."),
    ("dpx", REVIEW_IMAGE, "DPX frames", None, False, "stable", "per_frame", "One blob per frame."),
    ("mp4", REVIEW_MOVIE, "MP4 movie", None, False, "stable", "whole", "Compressed video changes on every frame; chunking saves nothing."),
    ("mov", REVIEW_MOVIE, "QuickTime movie", None, False, "stable", "whole", "Compressed video changes on every frame; chunking saves nothing."),
]

CATALOG: dict[str, dict] = {
    ext: {
        "extension": ext,
        "kind": kind,
        "label": label,
        "dcc": dcc,
        "canonicalizer": canon,
        "status": status,
        "recommended": rec,
        "strategies": list(STRATEGIES[kind]),
        "notes": notes,
    }
    for ext, kind, label, dcc, canon, status, rec, notes in _ENTRIES
}
