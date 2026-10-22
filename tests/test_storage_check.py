"""Storage integrity check: finds missing files, follows chunk lists, classifies, stays per studio."""
from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest
from blake3 import blake3

import app.config as config
from app.cas.factory import get_cas_backend
from app.reviews import processing
from app.storage_check.service import find_orphans
from tests.conftest import TEST_DB, invite_and_accept, make_project, signup

pytestmark = pytest.mark.skipif(not TEST_DB, reason="ZENO_TEST_DATABASE_URL not set")


@pytest.fixture(autouse=True)
def cas(tmp_path, monkeypatch):
    root = tmp_path / "cas"
    root.mkdir()
    monkeypatch.setattr(config, "CAS_ROOT", str(root))
    monkeypatch.setattr(config, "CAS_STORAGE_BACKEND", "nas")
    monkeypatch.setattr(config, "MONGO_URI", None)
    return root


async def _put(client, who, body: bytes) -> str:
    h = blake3(body).hexdigest()
    r = await client.put(f"/api/v1/cas/blobs/{h}", content=body, headers=who["headers"])
    assert r.status_code in (200, 201), r.text
    return h


def _manifest(chunks=None, segments=None, schema="chimera.manifest.v2") -> bytes:
    data = {"schema": schema, "filename": "x", "size": 1, "chunking": {"algo": "fastcdc"}}
    if chunks is not None:
        data["chunks"] = [{"hash": h, "size": 1} for h in chunks]
    if segments is not None:
        data["segments"] = segments
    return json.dumps(data).encode()


async def _register(client, who, project_code, asset, content_id, metadata=None):
    body = {"project": project_code, "asset": asset, "representation": "blend", "version": "next",
            "content_id": content_id, "filename": f"{asset}.blend", "size": 10}
    if metadata:
        body["metadata"] = metadata
    r = await client.post("/api/v1/versions", json=body, headers=who["headers"])
    assert r.status_code == 201, r.text
    return r.json()


async def _check(client, who):
    r = await client.post("/api/v1/storage-check", headers=who["headers"])
    assert r.status_code == 200, r.text
    return r.json()


def _problem(report, label_part):
    return next(p for p in report["problems"] if label_part in p["label"])


async def test_storage_check_finds_and_classifies_missing_blobs(client, tmp_path):
    a = await signup(client, "Alpha")
    b = await signup(client, "Beta")
    p = await make_project(client, a, code="CHK")
    for code in ("chunky", "dual", "patched"):
        await client.post(f"/api/v1/projects/{p['id']}/assets",
                          json={"type": "prop", "name": code, "code": code, "apply_template": False}, headers=a["headers"])

    # v2 chunk list with two chunks.
    c1, c2 = await _put(client, a, b"chunk-one"), await _put(client, a, b"chunk-two")
    m1 = await _put(client, a, _manifest(chunks=[c1, c2]))
    await _register(client, a, "CHK", "chunky", m1)
    # Dual artifact: whole file + optional dedup chunk list.
    raw = await _put(client, a, b"whole blend file")
    c3 = await _put(client, a, b"chunk-three")
    dedup = await _put(client, a, _manifest(chunks=[c3], schema="chimera.manifest.v3", segments=[{"kind": "raw_chunk", "hash": c3, "size": 1}]))
    await _register(client, a, "CHK", "dual", raw, {"dedup_artifact": {"content_id": dedup, "schema": "chimera.manifest.v3"}})
    # v3 chunk list that patches against the first chunk list (parent).
    patch, dic = await _put(client, a, b"patch"), await _put(client, a, b"dict")
    m3 = await _put(client, a, _manifest(schema="chimera.manifest.v3", segments=[
        {"kind": "zstd_dict_patch", "parent_content_id": m1, "dict_hash": dic, "patch_hash": patch,
         "range_start": 0, "range_end": 1, "patch_size": 1, "uncompressed_size": 1}]))
    await _register(client, a, "CHK", "patched", m3)
    # A review with previews.
    task = (await client.post("/api/v1/tasks", json={"project_id": p["id"], "type": "model", "title": "Model"}, headers=a["headers"])).json()
    clip = tmp_path / "c.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=s=160x90:r=24:d=0.5", "-pix_fmt", "yuv420p", str(clip)], check=True)
    with open(clip, "rb") as f:
        item = (await client.post("/api/v1/reviews/items", data={"task_id": task["id"]},
                                  files={"file": ("c.mp4", f, "video/mp4")}, headers=a["headers"])).json()
    await processing.wait_idle()

    report = await _check(client, a)
    assert report["ok"] and report["summary"]["versions"] == 3 and report["summary"]["reviews"] == 1
    assert report["summary"]["blobs_checked"] >= 12

    backend = get_cas_backend()
    backend.delete(c2)  # a chunk of v1 -> chunky broken, and patched (its parent) broken too
    backend.delete(dedup)  # optional dedup list -> dual degraded
    from app.db import acquire

    async with acquire() as conn:
        thumb_id = await conn.fetchval("SELECT thumb_content_id FROM review_items WHERE id = $1", __import__("uuid").UUID(item["id"]))
    backend.delete(thumb_id)  # preview -> degraded, rebuildable

    report = await _check(client, a)
    s = report["summary"]
    assert (s["broken"], s["degraded"], s["missing_blobs"]) == (2, 2, 3)
    chunky = _problem(report, "chunky")
    assert chunky["severity"] == "broken" and chunky["missing"] == [{"role": "chunk", "hash": c2}]
    assert "Republish" in chunky["fix"]
    patched = _problem(report, "patched")
    assert patched["severity"] == "broken" and {"role": "chunk", "hash": c2} in patched["missing"]
    dual = _problem(report, "dual")
    assert dual["severity"] == "degraded" and dual["missing"] == [{"role": "dedup chunk list", "hash": dedup}]
    review = next(p for p in report["problems"] if p["kind"] == "review")
    assert review["severity"] == "degraded" and review["can_rebuild"] and review["missing"][0]["role"] == "thumbnail"
    assert report["problems"][0]["severity"] == "broken"  # broken first

    # The last report is kept; rebuilding previews fixes the review.
    assert (await client.get("/api/v1/storage-check", headers=a["headers"])).json()["summary"]["broken"] == 2
    await client.post(f"/api/v1/reviews/items/{item['id']}/reprocess", headers=a["headers"])
    await processing.wait_idle()
    report = await _check(client, a)
    assert not any(p["kind"] == "review" for p in report["problems"])

    # Per studio and pipeline-only.
    other = await _check(client, b)
    assert other["ok"] and other["summary"]["versions"] == 0
    artist = await invite_and_accept(client, a, role="artist")
    assert (await client.post("/api/v1/storage-check", headers=artist["headers"])).status_code == 403

    # Orphans: a stray blob nothing references.
    stray = await _put(client, a, os.urandom(32))
    orphans = await find_orphans()
    assert stray in orphans["orphans"] and m1 not in orphans["orphans"]
