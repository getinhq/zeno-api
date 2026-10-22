"""Signed, expiring URLs for review media, served with HTTP Range support.

``<video>`` and ``<img>`` elements cannot send an Authorization header, so review media is
fetched from ``/api/v1/review-media/{content_id}`` with an HMAC signature over
``content_id | mime | expiry | filename`` in the query string. The signature is the
credential (like an S3 presigned URL); it is minted only for callers who can see the item
(team members, or a valid client share link).
"""
from __future__ import annotations

import hashlib
import hmac
import re
import time
from typing import Optional
from urllib.parse import quote, urlencode

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse

from app.auth import service as auth_service
from app.cas.factory import get_cas_backend, is_cas_configured
from app.cas.paths import is_valid_hash

router = APIRouter(prefix="/api/v1/review-media", tags=["reviews"])

URL_TTL_SECONDS = 6 * 3600
ALLOWED_MIME = {"video/mp4", "image/jpeg", "image/png", "application/octet-stream"}
_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _signature(content_id: str, mime: str, exp: int, filename: str) -> str:
    key = hashlib.sha256(("review-media:" + auth_service._secret()).encode()).digest()
    msg = f"{content_id}|{mime}|{exp}|{filename}".encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def sign(
    content_id: Optional[str],
    mime: str,
    *,
    filename: str = "",
    ttl: int = URL_TTL_SECONDS,
) -> Optional[str]:
    """Relative URL for ``content_id`` (None when there is nothing to serve)."""
    if not content_id:
        return None
    # Round expiry to the hour so repeated requests yield cacheable, identical URLs.
    exp = (int(time.time()) + ttl) // 3600 * 3600 + 3600
    params = {"mime": mime, "exp": exp, "sig": _signature(content_id, mime, exp, filename)}
    if filename:
        params["filename"] = filename
    return f"/api/v1/review-media/{content_id}?{urlencode(params)}"


@router.api_route("/{content_id}", methods=["GET", "HEAD"])
async def serve_media(
    request: Request,
    content_id: str,
    mime: str = Query(...),
    exp: int = Query(...),
    sig: str = Query(...),
    filename: str = Query(""),
):
    if not is_valid_hash(content_id) or mime not in ALLOWED_MIME:
        raise HTTPException(status_code=404, detail="Not found")
    if not hmac.compare_digest(sig, _signature(content_id, mime, exp, filename)):
        raise HTTPException(status_code=403, detail="Invalid media signature")
    if exp < time.time():
        raise HTTPException(status_code=403, detail="Media link expired; reload the page")
    if not is_cas_configured():
        raise HTTPException(status_code=503, detail="CAS not configured")
    backend = get_cas_backend()
    try:
        size = backend.get_size(content_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Media not found") from None

    headers = {
        "Accept-Ranges": "bytes",
        # Content-addressed: the bytes behind a hash never change.
        "Cache-Control": "private, max-age=86400",
        "ETag": f'"{content_id}"',
    }
    if filename:
        headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(filename)}"

    start, end = 0, size - 1
    status = 200
    range_header = request.headers.get("range")
    if range_header and size > 0:
        m = _RANGE_RE.match(range_header.strip())
        if not m or (not m.group(1) and not m.group(2)):
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        if m.group(1):
            start = int(m.group(1))
            end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
        else:  # suffix range: last N bytes
            start = max(0, size - int(m.group(2)))
        if start > end or start >= size:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        status = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    headers["Content-Length"] = str(end - start + 1)

    if request.method == "HEAD":
        return Response(status_code=status, headers=headers, media_type=mime)

    def body():
        if status == 206:
            yield from backend.get_range(content_id, start, end)
        else:
            yield from backend.get_stream(content_id)

    return StreamingResponse(body(), status_code=status, headers=headers, media_type=mime)
