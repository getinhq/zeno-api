"""ffprobe/ffmpeg helpers for review media.

Every video becomes an H.264 MP4 proxy built for frame-accurate browser playback:

- constant frame rate, timestamps rewritten to frame_index / fps starting at 0, so the
  player can map frame <-> time exactly (zeno-dashboard src/lib/frameController.js);
- no B-frames and a short GOP, so seeking and single-frame steps decode quickly;
- at most 1920x1080, yuv420p, tagged BT.709, ``+faststart`` for progressive loading.

Stills (PNG/JPEG/TIFF/EXR...) become a single PNG proxy; EXR is converted from linear to
sRGB. Thumbnails are a mid-clip JPEG; the filmstrip is one JPEG strip of evenly spaced
frames used for hover-scrubbing the timeline.
"""
from __future__ import annotations

import asyncio
import json
import math
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Optional

PROXY_MAX_W = 1920
PROXY_MAX_H = 1080
THUMB_W = 480
FILMSTRIP_TILE_W = 160
FILMSTRIP_MAX_TILES = 40

IMAGE_CODECS = {"png", "mjpeg", "jpeg2000", "tiff", "exr", "bmp", "webp", "dpx", "targa", "sgi", "hdr"}
LINEAR_CODECS = {"exr", "hdr"}


class MediaError(Exception):
    """The file could not be read or converted; the message is shown to the uploader."""


@dataclass
class ProbeResult:
    kind: str  # 'video' | 'image'
    codec: str
    width: int
    height: int
    fps: Fraction
    frame_count: Optional[int]
    duration: Optional[float]
    has_audio: bool
    info: dict = field(default_factory=dict)


async def _run(*args: str, timeout: float = 3600) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise MediaError(f"{Path(args[0]).name} timed out") from None
    if proc.returncode != 0:
        tail = err.decode("utf-8", "replace").strip().splitlines()[-3:]
        raise MediaError(f"{Path(args[0]).name} failed: {' / '.join(tail) or proc.returncode}")
    return out


def _fraction(text: Optional[str]) -> Optional[Fraction]:
    if not text or text in ("0/0", "0"):
        return None
    try:
        value = Fraction(text)
    except (ValueError, ZeroDivisionError):
        return None
    return value if value > 0 else None


def normalize_fps(value: Fraction) -> Fraction:
    """Snap near-misses (e.g. 23.9760239) onto the standard NTSC/film rates."""
    for std in (Fraction(24000, 1001), Fraction(30000, 1001), Fraction(60000, 1001)):
        if abs(float(value) - float(std)) < 0.001:
            return std
    if abs(float(value) - round(float(value))) < 0.001:
        return Fraction(round(float(value)))
    return value.limit_denominator(1001)


async def probe(path: Path) -> ProbeResult:
    out = await _run(
        "ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path),
        timeout=120,
    )
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise MediaError("ffprobe returned unreadable output") from exc
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video:
        raise MediaError("No video or image stream found in the file")
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    codec = str(video.get("codec_name") or "")
    fmt = data.get("format") or {}
    fmt_name = str(fmt.get("format_name") or "")
    nb_frames = video.get("nb_frames")
    is_image = codec in IMAGE_CODECS and (
        "image2" in fmt_name or "_pipe" in fmt_name or str(nb_frames or "1") == "1"
    )
    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    if width <= 0 or height <= 0:
        raise MediaError("Could not read the image size")
    info = {
        "codec": codec,
        "format": fmt_name,
        "pix_fmt": video.get("pix_fmt"),
        "color_space": video.get("color_space"),
        "color_transfer": video.get("color_transfer"),
        "color_primaries": video.get("color_primaries"),
        "bit_rate": fmt.get("bit_rate"),
    }
    if is_image:
        return ProbeResult("image", codec, width, height, Fraction(1), 1, None, False, info)

    fps = _fraction(video.get("avg_frame_rate")) or _fraction(video.get("r_frame_rate"))
    if fps is None:
        raise MediaError("Could not read the frame rate")
    fps = normalize_fps(fps)
    duration = None
    for raw in (video.get("duration"), fmt.get("duration")):
        try:
            duration = float(raw)
            break
        except (TypeError, ValueError):
            continue
    frame_count = int(nb_frames) if str(nb_frames or "").isdigit() else None
    if frame_count is None and duration:
        frame_count = max(1, round(duration * float(fps)))
    return ProbeResult("video", codec, width, height, fps, frame_count, duration, has_audio, info)


def _scale_filter(max_w: int = PROXY_MAX_W, max_h: int = PROXY_MAX_H) -> str:
    return (
        f"scale=w='min({max_w},iw)':h='min({max_h},ih)'"
        ":force_original_aspect_ratio=decrease:force_divisible_by=2"
    )


async def make_video_proxy(
    src: Path | str, dst: Path, fps: Fraction, has_audio: bool, *, input_args: list[str] | None = None
) -> None:
    """``input_args`` go before ``-i`` (e.g. ``-framerate``/``-start_number`` for an image sequence)."""
    rate = f"{fps.numerator}/{fps.denominator}"
    gop = str(max(1, round(float(fps) / 2)))
    vf = f"fps={rate},setpts=N/FRAME_RATE/TB,{_scale_filter()},format=yuv420p"
    args = [
        "ffmpeg", "-nostdin", "-loglevel", "error", "-y", *(input_args or []), "-i", str(src),
        "-map", "0:v:0", "-vf", vf, "-fps_mode", "cfr", "-r", rate,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-g", gop, "-keyint_min", gop,
        "-sc_threshold", "0", "-bf", "0", "-pix_fmt", "yuv420p",
        "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
    ]
    if has_audio:
        args += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "160k", "-af", "aresample=async=1"]
    args += ["-movflags", "+faststart", str(dst)]
    await _run(*args)


async def count_frames(path: Path) -> int:
    """Exact frame count of a proxy (one packet per frame: no B-frames, CFR)."""
    out = await _run(
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
        "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(path),
        timeout=300,
    )
    text = out.decode().strip().split(",")[0]
    if not text.isdigit() or int(text) <= 0:
        raise MediaError("The converted video has no frames")
    return int(text)


async def make_image_proxy(src: Path, dst: Path, codec: str) -> None:
    pre = ["-apply_trc", "iec61966_2_1"] if codec in LINEAR_CODECS else []
    await _run(
        "ffmpeg", "-nostdin", "-loglevel", "error", "-y", *pre, "-i", str(src),
        "-frames:v", "1", "-vf", f"{_scale_filter(3840, 2160)},format=rgb24",
        str(dst),
        timeout=300,
    )


async def make_thumbnail(proxy: Path, dst: Path, *, at_seconds: float = 0.0) -> None:
    await _run(
        "ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-ss", f"{max(0.0, at_seconds):.3f}",
        "-i", str(proxy), "-frames:v", "1", "-vf", f"scale={THUMB_W}:-2", "-q:v", "4", str(dst),
        timeout=300,
    )


def filmstrip_layout(frame_count: int) -> tuple[int, int]:
    """(tiles, step): one tile every ``step`` frames, at most FILMSTRIP_MAX_TILES."""
    step = max(1, math.ceil(frame_count / FILMSTRIP_MAX_TILES))
    return math.ceil(frame_count / step), step


async def make_filmstrip(proxy: Path, dst: Path, frame_count: int) -> tuple[int, int]:
    tiles, step = filmstrip_layout(frame_count)
    vf = f"select='not(mod(n\\,{step}))',scale={FILMSTRIP_TILE_W}:-2,tile={tiles}x1"
    await _run(
        "ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(proxy),
        "-vf", vf, "-fps_mode", "vfr", "-frames:v", "1", "-q:v", "5", str(dst),
    )
    return tiles, step


def sequence_input_args(fps: Fraction, codec: str) -> list[str]:
    """ffmpeg input options for a numbered image sequence (EXR/HDR converted to sRGB)."""
    args = ["-framerate", f"{fps.numerator}/{fps.denominator}", "-start_number", "0"]
    if codec in LINEAR_CODECS:
        args = ["-apply_trc", "iec61966_2_1", *args]
    return args
