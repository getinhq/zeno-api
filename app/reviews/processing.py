"""Background conversion of uploaded review media into browser proxies.

Runs inside the API process: ``enqueue(item_id)`` schedules the job on the event loop and
at most ``ZENO_REVIEW_WORKERS`` ffmpeg jobs run at once. Jobs left ``queued``/``processing``
by a restart are picked up again by ``resume_pending()`` at startup. Small studios do not
need a separate worker service; a busy server can run more API replicas.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
from fractions import Fraction
from pathlib import Path
from typing import Optional
from uuid import UUID

from blake3 import blake3

from app.cas.factory import get_cas_backend
from app.db import acquire
from app.reviews import media

log = logging.getLogger(__name__)

_WORKERS = max(1, int(os.environ.get("ZENO_REVIEW_WORKERS", "1")))
_semaphore: Optional[asyncio.Semaphore] = None
_tasks: set[asyncio.Task] = set()


def _sem() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(_WORKERS)
    return _semaphore


def _hash_file(path: Path) -> str:
    h = blake3()
    with open(path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def store_file(path: Path) -> str:
    """Put a local file into CAS; returns its content id."""
    content_id = _hash_file(path)
    get_cas_backend().put_from_path(content_id, path)
    return content_id


def _materialize(content_id: str, workdir: Path, suffix: str) -> Path:
    """Local path to a CAS blob: the blob itself on disk backends, else a temp copy."""
    backend = get_cas_backend()
    get_path = getattr(backend, "get_path", None)
    if get_path is not None:
        p = Path(get_path(content_id))
        if p.is_file():
            return p
    dst = workdir / f"source{suffix}"
    with open(dst, "wb") as f:
        for chunk in backend.get_stream(content_id):
            f.write(chunk)
    return dst


def _link_blob(content_id: str, dst: Path, cache: dict[str, Path]) -> None:
    """Make ``dst`` a local copy of a CAS blob: a symlink on disk backends, else one download."""
    src = cache.get(content_id)
    if src is None:
        backend = get_cas_backend()
        get_path = getattr(backend, "get_path", None)
        p = Path(get_path(content_id)) if get_path else None
        if p is not None and p.is_file():
            src = p
        else:
            src = dst.parent.parent / "blobs" / content_id
            src.parent.mkdir(exist_ok=True)
            with open(src, "wb") as f:
                for chunk in backend.get_stream(content_id):
                    f.write(chunk)
        cache[content_id] = src
    os.symlink(src, dst)


def _lay_out_sequence(frames: list[dict], workdir: Path, ext: str) -> tuple[Path, list[int]]:
    """Number frames 0..N in a folder for ffmpeg; gaps hold the previous frame. Returns (dir, missing)."""
    seq = workdir / "seq"
    seq.mkdir()
    by_frame = {int(f["frame"]): f["content_id"] for f in frames}
    first, last = min(by_frame), max(by_frame)
    cache: dict[str, Path] = {}
    missing: list[int] = []
    current = by_frame[first]
    for i, frame in enumerate(range(first, last + 1)):
        if frame in by_frame:
            current = by_frame[frame]
        else:
            missing.append(frame)
        _link_blob(current, seq / f"{i:06d}{ext}", cache)
    return seq, missing


def enqueue(item_id: UUID | str) -> None:
    task = asyncio.get_running_loop().create_task(_guarded(UUID(str(item_id))))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def resume_pending() -> int:
    async with acquire() as conn:
        rows = await conn.fetch(
            "SELECT id FROM review_items WHERE processing_status IN ('queued','processing') ORDER BY created_at"
        )
    for r in rows:
        enqueue(r["id"])
    return len(rows)


async def wait_idle() -> None:
    """Wait for every running job (tests)."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


async def _guarded(item_id: UUID) -> None:
    async with _sem():
        try:
            await process_item(item_id)
        except Exception as exc:  # noqa: BLE001
            log.exception("review media processing failed for %s", item_id)
            msg = str(exc) if isinstance(exc, media.MediaError) else f"Processing failed: {type(exc).__name__}"
            async with acquire() as conn:
                await conn.execute(
                    "UPDATE review_items SET processing_status = 'failed', processing_error = $2 WHERE id = $1",
                    item_id,
                    msg[:500],
                )


async def process_item(item_id: UUID) -> None:
    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE review_items SET processing_status = 'processing', processing_error = NULL
            WHERE id = $1
            RETURNING kind, frames, source_info, source_content_id, source_filename, fps_num, fps_den
            """,
            item_id,
        )
    if not row:
        return
    backend = get_cas_backend()
    # Disk CAS stores files by rename, so derived files must be on the CAS volume.
    base = backend.ensure_tmp() if hasattr(backend, "get_path") else None
    workdir = Path(tempfile.mkdtemp(prefix="zeno_review_", dir=base))
    try:
        if row["kind"] == "sequence":
            await _process_sequence(item_id, row, workdir)
            return
        suffix = Path(row["source_filename"]).suffix.lower()[:10]
        src = await asyncio.to_thread(_materialize, row["source_content_id"], workdir, suffix)
        info = await media.probe(src)
        updates: dict = {
            "kind": info.kind,
            "width": info.width,
            "height": info.height,
            "has_audio": info.has_audio,
            "source_info": json.dumps(info.info),
        }
        if info.kind == "image":
            proxy = workdir / "proxy.png"
            await media.make_image_proxy(src, proxy, info.codec)
            thumb = workdir / "thumb.jpg"
            await media.make_thumbnail(proxy, thumb)
            updates.update(
                proxy_mime="image/png",
                frame_count=1,
                fps_num=row["fps_num"] or 24,
                fps_den=row["fps_den"] or 1,
            )
        else:
            # An uploader-supplied rate wins (e.g. a 30 fps container holding a 24 fps edit).
            fps = Fraction(row["fps_num"], row["fps_den"]) if row["fps_num"] and row["fps_den"] else info.fps
            proxy = workdir / "proxy.mp4"
            await media.make_video_proxy(src, proxy, fps, info.has_audio)
            frame_count = await media.count_frames(proxy)
            thumb = workdir / "thumb.jpg"
            await media.make_thumbnail(proxy, thumb, at_seconds=(frame_count / 2) / float(fps))
            strip = workdir / "filmstrip.jpg"
            tiles, step = await media.make_filmstrip(proxy, strip, frame_count)
            updates.update(
                proxy_mime="video/mp4",
                frame_count=frame_count,
                fps_num=fps.numerator,
                fps_den=fps.denominator,
                filmstrip_content_id=await asyncio.to_thread(store_file, strip),
                filmstrip_tiles=tiles,
                filmstrip_step=step,
            )
        updates["proxy_content_id"] = await asyncio.to_thread(store_file, proxy)
        updates["thumb_content_id"] = await asyncio.to_thread(store_file, thumb)
        await _save(item_id, updates)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def _save(item_id: UUID, updates: dict) -> None:
    cols = list(updates)
    assignments = ", ".join(
        f"{c} = ${i + 2}{'::jsonb' if c == 'source_info' else ''}" for i, c in enumerate(cols)
    )
    async with acquire() as conn:
        await conn.execute(
            f"UPDATE review_items SET {assignments}, processing_status = 'ready' WHERE id = $1",
            item_id,
            *[updates[c] for c in cols],
        )


async def _make_video_outputs(proxy: Path, workdir: Path, fps: Fraction) -> dict:
    frame_count = await media.count_frames(proxy)
    thumb = workdir / "thumb.jpg"
    await media.make_thumbnail(proxy, thumb, at_seconds=(frame_count / 2) / float(fps))
    strip = workdir / "filmstrip.jpg"
    tiles, step = await media.make_filmstrip(proxy, strip, frame_count)
    return {
        "proxy_mime": "video/mp4",
        "frame_count": frame_count,
        "fps_num": fps.numerator,
        "fps_den": fps.denominator,
        "filmstrip_content_id": await asyncio.to_thread(store_file, strip),
        "filmstrip_tiles": tiles,
        "filmstrip_step": step,
        "thumb_content_id": await asyncio.to_thread(store_file, thumb),
    }


async def _process_sequence(item_id: UUID, row, workdir: Path) -> None:
    """Image sequence: frames stay one blob each in CAS; only the proxy is built from them."""
    frames = row["frames"] if isinstance(row["frames"], list) else json.loads(row["frames"] or "[]")
    source_info = row["source_info"] if isinstance(row["source_info"], dict) else json.loads(row["source_info"] or "{}")
    seq_info = dict(source_info.get("sequence") or {})
    ext = str(seq_info.get("ext") or ".png")
    seq_dir, missing = await asyncio.to_thread(_lay_out_sequence, frames, workdir, ext)
    info = await media.probe(seq_dir / f"{0:06d}{ext}")
    fps = Fraction(row["fps_num"], row["fps_den"]) if row["fps_num"] and row["fps_den"] else Fraction(24)
    proxy = workdir / "proxy.mp4"
    await media.make_video_proxy(
        seq_dir / f"%06d{ext}", proxy, fps, False, input_args=media.sequence_input_args(fps, info.codec)
    )
    updates = await _make_video_outputs(proxy, workdir, fps)
    updates["proxy_content_id"] = await asyncio.to_thread(store_file, proxy)
    seq_info["missing_frames"] = missing
    updates.update(
        width=info.width,
        height=info.height,
        has_audio=False,
        source_info=json.dumps({**info.info, "sequence": seq_info}),
    )
    await _save(item_id, updates)
