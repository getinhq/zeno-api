"""Review: submissions, proxy conversion, signed ranged media, notes across revisions,
decisions, permissions, studio isolation and client share links."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import app.config as config
from app.reviews import processing
from tests.conftest import TEST_DB, invite_and_accept, make_project, signup

pytestmark = [
    pytest.mark.skipif(not TEST_DB, reason="ZENO_TEST_DATABASE_URL not set"),
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed"),
]


@pytest.fixture(autouse=True)
def cas(tmp_path, monkeypatch):
    root = tmp_path / "cas"
    root.mkdir()
    monkeypatch.setattr(config, "CAS_ROOT", str(root))
    monkeypatch.setattr(config, "CAS_STORAGE_BACKEND", "nas")
    monkeypatch.setattr(config, "MONGO_URI", None)
    return root


def _clip(path: Path, *, rate: str = "24", seconds: float = 2, audio: bool = False) -> Path:
    """Test clip whose frames encode their own index as stripes."""
    args = ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"color=c=gray:s=320x180:r={rate}:d={seconds}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    args += ["-vf", "geq=lum='255*mod(floor(N/pow(2,floor(X/32))),2)':cb=128:cr=128", "-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if audio:
        args += ["-c:a", "aac", "-shortest"]
    subprocess.run(args + [str(path)], check=True)
    return path


async def _task(client, owner, project_id):
    r = await client.post(
        f"/api/v1/projects/{project_id}/assets",
        json={"type": "character", "name": "Nono", "code": "nono"},
        headers=owner["headers"],
    )
    tasks = (await client.get(f"/api/v1/tasks?asset_id={r.json()['id']}", headers=owner["headers"])).json()
    return next(t for t in tasks if t["type"] == "rig")


async def _submit(client, who, task_id, path: Path, **form):
    with open(path, "rb") as f:
        r = await client.post(
            "/api/v1/reviews/items",
            data={"task_id": task_id, **{k: str(v) for k, v in form.items()}},
            files={"file": (path.name, f, "application/octet-stream")},
            headers=who["headers"],
        )
    return r


async def _ready(client, who, item_id):
    await processing.wait_idle()
    r = await client.get(f"/api/v1/reviews/items/{item_id}", headers=who["headers"])
    assert r.status_code == 200, r.text
    return r.json()


async def test_submit_converts_to_frame_exact_proxy(client, tmp_path):
    a = await signup(client, "Alpha")
    p = await make_project(client, a)
    task = await _task(client, a, p["id"])
    r = await _submit(client, a, task["id"], _clip(tmp_path / "pb.mov", audio=True), frame_start=1001, description="blocking pass")
    assert r.status_code == 201, r.text
    item = r.json()
    assert item["label"] == "v001" and item["processing_status"] in ("queued", "processing")

    item = await _ready(client, a, item["id"])
    assert item["processing_status"] == "ready", item["processing_error"]
    assert (item["fps"], item["frame_count"], item["frame_start"], item["frame_end"]) == (24, 48, 1001, 1048)
    assert item["has_audio"] and item["kind"] == "video"
    assert item["task"]["status"] == "review"
    assert item["filmstrip_tiles"] >= 1

    # Proxy: signed URL, ranged reads, constant frame rate starting at t=0.
    url = item["media"]["proxy"]
    head = await client.head(url)
    assert head.status_code == 200 and head.headers["accept-ranges"] == "bytes"
    size = int(head.headers["content-length"])
    part = await client.get(url, headers={"Range": "bytes=100-199"})
    assert part.status_code == 206 and len(part.content) == 100
    assert part.headers["content-range"] == f"bytes 100-199/{size}"
    tail = await client.get(url, headers={"Range": "bytes=-50"})
    assert tail.status_code == 206 and len(tail.content) == 50
    assert (await client.get(url, headers={"Range": f"bytes={size}-"})).status_code == 416
    full = await client.get(url)
    assert full.status_code == 200 and len(full.content) == size
    proxy = tmp_path / "proxy.mp4"
    proxy.write_bytes(full.content)
    pts = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time", "-of", "csv=p=0", str(proxy)],
        check=True, capture_output=True, text=True,
    ).stdout.split()
    times = sorted(float(t) for t in pts)
    assert len(times) == 48 and times[0] == 0.0
    assert all(abs(t - i / 24) < 1e-3 for i, t in enumerate(times))

    # Tampered or foreign signatures are refused; the thumbnail is served as JPEG.
    assert (await client.get(url.replace("sig=", "sig=0"))).status_code == 403
    thumb = await client.get(item["media"]["thumbnail"])
    assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/jpeg"
    original = await client.get(item["media"]["original"])
    assert "attachment" in original.headers["content-disposition"]


async def test_ntsc_rate_and_fps_override(client, tmp_path):
    a = await signup(client, "Alpha")
    p = await make_project(client, a)
    task = await _task(client, a, p["id"])
    item = (await _submit(client, a, task["id"], _clip(tmp_path / "ntsc.mp4", rate="24000/1001"))).json()
    item = await _ready(client, a, item["id"])
    assert (item["fps_num"], item["fps_den"]) == (24000, 1001)
    # Uploader says the edit is 12 fps: the proxy is retimed to that rate.
    item2 = (await _submit(client, a, task["id"], _clip(tmp_path / "x.mp4"), fps="12")).json()
    item2 = await _ready(client, a, item2["id"])
    assert item2["fps"] == 12 and item2["frame_count"] == 24 and item2["label"] == "v002"


async def test_still_image_and_rejections(client, tmp_path):
    a = await signup(client, "Alpha")
    p = await make_project(client, a)
    task = await _task(client, a, p["id"])
    png = tmp_path / "frame.png"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=s=640x360", "-frames:v", "1", str(png)], check=True)
    item = await _ready(client, a, (await _submit(client, a, task["id"], png)).json()["id"])
    assert item["kind"] == "image" and item["frame_count"] == 1 and item["media"]["proxy"]

    bad = tmp_path / "notes.txt"
    bad.write_text("hi")
    assert (await _submit(client, a, task["id"], bad)).status_code == 415
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"not a video at all")
    item = await _ready(client, a, (await _submit(client, a, task["id"], broken)).json()["id"])
    assert item["processing_status"] == "failed" and item["processing_error"]


async def test_notes_follow_the_task_to_the_fix(client, tmp_path):
    sup = await signup(client, "Alpha")  # studio owner: management role
    artist = await invite_and_accept(client, sup, role="artist")
    p = await make_project(client, sup)
    task = await _task(client, sup, p["id"])
    await client.patch(f"/api/v1/tasks/{task['id']}", json={"assignee_id": artist["user"]["id"]}, headers=sup["headers"])

    v1 = await _ready(client, artist, (await _submit(client, artist, task["id"], _clip(tmp_path / "a.mp4"), frame_start=1001)).json()["id"])
    drawing = {"shapes": [{"tool": "arrow", "color": "#ff3b30", "width": 3, "points": [[0.2, 0.3], [0.5, 0.5]]}]}
    r = await client.post(
        f"/api/v1/reviews/items/{v1['id']}/notes",
        json={"body": "Foot slides here", "frame": 1012, "frame_end": 1018, "drawing": drawing},
        headers=sup["headers"],
    )
    assert r.status_code == 201, r.text
    note_id = r.json()["id"]
    await client.post(
        f"/api/v1/reviews/items/{v1['id']}/notes",
        json={"body": "Which foot?", "parent_id": note_id}, headers=artist["headers"],
    )
    await client.post(f"/api/v1/reviews/items/{v1['id']}/notes", json={"body": "Nice timing", "actionable": False}, headers=sup["headers"])

    # Artists cannot decide; supervisors can.
    assert (await client.post(f"/api/v1/reviews/items/{v1['id']}/decision", json={"decision": "approve"}, headers=artist["headers"])).status_code == 403
    r = await client.post(f"/api/v1/reviews/items/{v1['id']}/decision", json={"decision": "changes_requested", "comment": "One fix"}, headers=sup["headers"])
    assert r.status_code == 200 and r.json()["review_status"] == "changes_requested"
    t = (await client.get(f"/api/v1/tasks/{task['id']}", headers=sup["headers"])).json()
    assert t["status"] == "in_progress"
    bell = (await client.get(f"/api/v1/notifications?project_id={p['id']}", headers=artist["headers"])).json()
    assert any(n["kind"] == "review.decided" for n in (bell.get("items", bell) if isinstance(bell, dict) else bell))

    # v002 arrives: the open note is shown on it, raised in v001.
    v2 = await _ready(client, artist, (await _submit(client, artist, task["id"], _clip(tmp_path / "b.mp4"), frame_start=1001)).json()["id"])
    assert v2["label"] == "v002" and v2["open_notes"] == 1
    assert [r["label"] for r in v2["revisions"]] == ["v002", "v001"]
    note = next(n for n in v2["notes"] if n["id"] == note_id)
    assert (note["raised_in"], note["frame"], note["frame_end"], note["status"]) == ("v001", 1012, 1018, "open")
    assert note["drawing"] == drawing and note["replies"][0]["body"] == "Which foot?"
    general = next(n for n in v2["notes"] if n["body"] == "Nice timing")
    assert general["status"] is None
    assert any(n["kind"] == "decision" for n in v2["notes"])

    # Artist marks it fixed (defaults to the newest revision) but cannot verify it.
    r = await client.patch(f"/api/v1/reviews/notes/{note_id}", json={"status": "fixed", "comment": "Planted the heel"}, headers=artist["headers"])
    assert r.status_code == 200, r.text
    fixed = next(n for n in r.json()["notes"] if n["id"] == note_id)
    assert fixed["status"] == "fixed" and fixed["fixed_in"] == "v002"
    assert fixed["replies"][-1]["kind"] == "status" and fixed["replies"][-1]["meta"] == {"from": "open", "to": "fixed"}
    assert (await client.patch(f"/api/v1/reviews/notes/{note_id}", json={"status": "verified"}, headers=artist["headers"])).status_code == 403
    unresolved = (await client.get(f"/api/v1/reviews/notes?task_id={task['id']}&status=unresolved", headers=artist["headers"])).json()
    assert [n["id"] for n in unresolved] == [note_id]

    # Approval verifies fixed notes, completes the task.
    r = await client.post(f"/api/v1/reviews/items/{v2['id']}/decision", json={"decision": "approve"}, headers=sup["headers"])
    assert r.json()["review_status"] == "approved"
    assert (await client.get(f"/api/v1/tasks/{task['id']}", headers=sup["headers"])).json()["status"] == "completed"
    final = await _ready(client, sup, v2["id"])
    assert next(n for n in final["notes"] if n["id"] == note_id)["status"] == "verified"
    queue = (await client.get(f"/api/v1/reviews/items?project_id={p['id']}&latest_only=true&review_status=pending", headers=sup["headers"])).json()
    assert queue == []

    # Only the author edits a note body; reopening is the reviewer's call.
    assert (await client.patch(f"/api/v1/reviews/notes/{note_id}", json={"body": "x"}, headers=artist["headers"])).status_code == 403
    assert (await client.patch(f"/api/v1/reviews/notes/{note_id}", json={"status": "open"}, headers=sup["headers"])).status_code == 200


async def test_studios_cannot_see_each_others_reviews(client, tmp_path):
    a = await signup(client, "Alpha")
    b = await signup(client, "Beta")
    p = await make_project(client, a)
    task = await _task(client, a, p["id"])
    item = (await _submit(client, a, task["id"], _clip(tmp_path / "a.mp4", seconds=0.5))).json()
    await processing.wait_idle()
    assert (await client.get(f"/api/v1/reviews/items/{item['id']}", headers=b["headers"])).status_code == 404
    assert (await _submit(client, b, task["id"], _clip(tmp_path / "b.mp4", seconds=0.5))).status_code == 404
    assert (await client.post(f"/api/v1/reviews/items/{item['id']}/notes", json={"body": "hi"}, headers=b["headers"])).status_code == 404
    assert (await client.get(f"/api/v1/reviews/items?project_id={p['id']}", headers=b["headers"])).status_code == 404
    assert (await client.post("/api/v1/reviews/shares", json={"item_ids": [item["id"]]}, headers=b["headers"])).status_code == 404
    # Without a token nothing is reachable (auth on).
    assert (await client.get(f"/api/v1/reviews/items/{item['id']}")).status_code == 401


async def test_client_share_link(client, tmp_path):
    sup = await signup(client, "Alpha")
    p = await make_project(client, sup)
    task = await _task(client, sup, p["id"])
    item = await _ready(client, sup, (await _submit(client, sup, task["id"], _clip(tmp_path / "a.mp4"), frame_start=1)).json()["id"])
    await client.post(f"/api/v1/reviews/items/{item['id']}/notes", json={"body": "internal only", "frame": 3}, headers=sup["headers"])

    r = await client.post(
        "/api/v1/reviews/shares",
        json={"item_ids": [item["id"]], "title": "Client cut", "password": "s3cret", "expires_in_days": 7},
        headers=sup["headers"],
    )
    assert r.status_code == 201, r.text
    share = r.json()
    token = share["link"].rsplit("/r/", 1)[1]
    base = f"/api/v1/review-shares/public/{token}"

    assert (await client.get(base)).status_code == 401
    assert (await client.get(base, headers={"X-Share-Password": "nope"})).status_code == 401
    pw = {"X-Share-Password": "s3cret"}
    data = (await client.get(base, headers=pw)).json()
    assert data["title"] == "Client cut" and len(data["items"]) == 1
    shared = data["items"][0]
    assert shared["notes"] == [] and "submitted_by" not in shared
    assert (await client.get(shared["media"]["proxy"], headers={"Range": "bytes=0-9"})).status_code == 206

    r = await client.post(f"{base}/items/{item['id']}/notes", json={"guest_name": "Dana (client)", "body": "Brighter sky", "frame": 10}, headers=pw)
    assert r.status_code == 201, r.text
    client_note = r.json()["id"]
    assert (await client.post(f"{base}/items/{item['id']}/notes", json={"body": "anon"}, headers=pw)).status_code == 400
    # Team replies to a client note; the client sees the reply, not internal notes.
    await client.post(f"/api/v1/reviews/items/{item['id']}/notes", json={"body": "On it", "parent_id": client_note}, headers=sup["headers"])
    r = await client.post(f"{base}/items/{item['id']}/decision", json={"guest_name": "Dana", "decision": "approve"}, headers=pw)
    assert r.status_code == 200
    data = (await client.get(base, headers=pw)).json()
    bodies = {n["body"] for n in data["items"][0]["notes"]}
    assert "Brighter sky" in bodies and "internal only" not in bodies
    thread = next(n for n in data["items"][0]["notes"] if n["id"] == client_note)
    assert [x["body"] for x in thread["replies"]] == ["On it"]
    # A client approval is recorded but the supervisor still decides.
    team_view = await _ready(client, sup, item["id"])
    assert team_view["review_status"] == "pending"
    assert any(n["kind"] == "decision" and n["is_guest"] for n in team_view["notes"])

    shares = (await client.get(f"/api/v1/reviews/shares?project_id={p['id']}", headers=sup["headers"])).json()
    assert shares[0]["state"] == "active" and shares[0]["last_viewed_at"]
    assert (await client.delete(f"/api/v1/reviews/shares/{share['id']}", headers=sup["headers"])).status_code == 204
    assert (await client.get(base, headers=pw)).status_code == 404
