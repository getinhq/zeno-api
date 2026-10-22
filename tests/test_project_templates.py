"""Project templates: skeleton creation, milestones, starter assets, validation, rollback."""
from __future__ import annotations

import pytest

from tests.conftest import TEST_DB, invite_and_accept, signup

pytestmark = pytest.mark.skipif(not TEST_DB, reason="ZENO_TEST_DATABASE_URL not set")


async def test_lists_templates(client):
    a = await signup(client, "Alpha")
    ids = [t["id"] for t in (await client.get("/api/v1/project-templates", headers=a["headers"])).json()]
    assert {"short_film", "series", "commercial", "music_video", "game_cinematic", "blank"} <= set(ids)


async def test_series_skeleton(client):
    a = await signup(client, "Alpha")
    h = a["headers"]
    r = await client.post(
        "/api/v1/projects/from-template",
        json={
            "template": "series",
            "name": "Nono Shorts",
            "code": "NONO",
            "start_date": "2027-01-04",
            "end_date": "2027-04-04",
            "structure": {"episodes": 2, "sequences_per_episode": 2, "shots_per_sequence": 3, "frames_per_shot": 48},
            "assets": {"character": ["Nono", "Grandma"], "prop": "Coffee mug, Kettle", "environment": ["Kitchen"]},
        },
        headers=h,
    )
    assert r.status_code == 201, r.text
    data = r.json()
    created = data["created"]
    assert created == {
        "episodes": 2, "sequences": 4, "shots": 12, "assets": 5,
        # 12 shots x 5 shot tasks + 2 characters x 5 + 2 props x 3 + 1 environment x 4
        "tasks": 60 + 10 + 6 + 4,
        "milestones": 4,
    }
    pid = data["project"]["id"]

    shots = (await client.get(f"/api/v1/projects/{pid}/shots", headers=h)).json()
    assert len(shots) == 12
    first = next(s for s in shots if s["shot_code"] == "SH0010")
    assert (first["frame_start"], first["frame_end"]) == (1001, 1048)

    eps = (await client.get(f"/api/v1/projects/{pid}/episodes", headers=h)).json()
    assert sorted(e["code"] for e in eps) == ["EP01", "EP02"]

    assets = (await client.get(f"/api/v1/projects/{pid}/assets", headers=h)).json()
    assert sorted(a["code"] for a in assets) == ["coffee_mug", "grandma", "kettle", "kitchen", "nono"]

    project = (await client.get(f"/api/v1/projects/{pid}", headers=h)).json()
    assert project["metadata"]["template"] == "series"
    assert project["end_date"] == "2027-04-04"


async def test_blank_and_validation(client):
    a = await signup(client, "Alpha")
    h = a["headers"]
    r = await client.post("/api/v1/projects/from-template", json={"template": "blank", "name": "B", "code": "B1"}, headers=h)
    assert r.status_code == 201
    assert r.json()["created"] == {"episodes": 0, "sequences": 0, "shots": 0, "assets": 0, "tasks": 0, "milestones": 0}

    bad = [
        {"template": "nope", "name": "X", "code": "X1"},
        {"template": "short_film", "name": "X", "code": "has space"},
        {"template": "short_film", "name": "X", "code": "X2", "start_date": "2027-02-01", "end_date": "2027-01-01"},
        {"template": "short_film", "name": "X", "code": "X3", "structure": {"episodes": 50, "sequences_per_episode": 50, "shots_per_sequence": 50}},
        {"template": "short_film", "name": "X", "code": "X4", "assets": {"spaceship": ["Y"]}},
    ]
    for body in bad:
        assert (await client.post("/api/v1/projects/from-template", json=body, headers=h)).status_code == 400, body


async def test_duplicate_code_rolls_back_everything(client):
    a = await signup(client, "Alpha")
    h = a["headers"]
    body = {"template": "commercial", "name": "Spot", "code": "SPOT"}
    assert (await client.post("/api/v1/projects/from-template", json=body, headers=h)).status_code == 201
    dup = await client.post("/api/v1/projects/from-template", json={**body, "name": "Spot 2"}, headers=h)
    assert dup.status_code == 409
    projects = (await client.get("/api/v1/projects?status=all", headers=h)).json()
    assert [p["code"] for p in projects] == ["SPOT"]


async def test_milestones_follow_project_dates(client):
    a = await signup(client, "Alpha")
    r = await client.post(
        "/api/v1/projects/from-template",
        json={"template": "short_film", "name": "Film", "code": "FILM",
              "start_date": "2027-01-01", "end_date": "2027-05-01", "structure": {"episodes": 0}},
        headers=a["headers"],
    )
    assert r.status_code == 201, r.text
    pid = r.json()["project"]["id"]
    from app.db import acquire

    async with acquire() as conn:
        rows = await conn.fetch("SELECT name, due_date::text AS d FROM milestones WHERE project_id = $1 ORDER BY due_date", pid)
    assert rows[-1]["name"] == "Final delivery" and rows[-1]["d"] == "2027-05-01"
    assert rows[0]["d"] == "2027-01-13"  # 10% of 120 days


async def test_artists_cannot_create_projects(client):
    a = await signup(client, "Alpha")
    artist = await invite_and_accept(client, a)
    r = await client.post("/api/v1/projects/from-template", json={"template": "blank", "name": "N", "code": "N1"}, headers=artist["headers"])
    assert r.status_code == 403
