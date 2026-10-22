"""Deleting a fully populated studio removes everything and touches nobody else."""
from __future__ import annotations

import pytest

from tests.conftest import PASSWORD, TEST_DB, invite_and_accept, make_project, signup

pytestmark = pytest.mark.skipif(not TEST_DB, reason="ZENO_TEST_DATABASE_URL not set")


async def test_delete_populated_studio(client):
    a = await signup(client, "Doomed")
    b = await signup(client, "Survivor")
    h = a["headers"]
    artist = await invite_and_accept(client, a)
    await client.post("/api/v1/invites", json={}, headers=h)
    await client.post("/api/v1/auth/tokens", json={"name": "blender"}, headers=artist["headers"])
    r = await client.post(
        "/api/v1/projects/from-template",
        json={"template": "short_film", "name": "Doomed Film", "code": "DOOM",
              "structure": {"sequences_per_episode": 1, "shots_per_sequence": 2},
              "assets": {"character": ["Hero"]}},
        headers=h,
    )
    pid = r.json()["project"]["id"]
    bd = (await client.get(f"/api/v1/projects/{pid}/breakdown", headers=h)).json()
    await client.post(
        f"/api/v1/projects/{pid}/breakdown",
        json={"add": [{"shot_id": s["id"], "asset_id": bd["assets"][0]["id"]} for s in bd["shots"]]},
        headers=h,
    )
    await client.post(f"/api/v1/projects/{pid}/schedule/auto", json={}, headers=h)
    task = (await client.get(f"/api/v1/projects/{pid}/schedule", headers=h)).json()["tasks"][0]
    await client.patch(f"/api/v1/tasks/{task['id']}", json={"assignees": [artist["user"]["id"]], "reviewer_id": artist["user"]["id"]}, headers=h)
    await client.post("/api/v1/issues", json={"title": "bug", "project_id": pid, "assignee_id": artist["user"]["id"]}, headers=h)
    pb = await make_project(client, b, code="SAFE")

    # Guards: artist cannot, wrong name cannot.
    assert (await client.request("DELETE", "/api/v1/organization", json={"confirm_name": a["user"]["organization"]["name"]}, headers=artist["headers"])).status_code == 403
    assert (await client.request("DELETE", "/api/v1/organization", json={"confirm_name": "nope"}, headers=h)).status_code == 400

    r = await client.request("DELETE", "/api/v1/organization", json={"confirm_name": a["user"]["organization"]["name"]}, headers=h)
    assert r.status_code == 200, r.text

    # Everything of the deleted studio is gone; sessions and tokens stop working.
    login = await client.post("/api/v1/auth/login", json={"username": a["user"]["email"], "password": PASSWORD})
    assert login.status_code == 401
    assert (await client.get("/api/v1/projects", headers=h)).status_code == 401
    from app.db import acquire

    async with acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM projects WHERE id = $1", pid) == 0
        assert await conn.fetchval("SELECT count(*) FROM tasks WHERE project_id = $1", pid) == 0
        assert await conn.fetchval("SELECT count(*) FROM users WHERE id = $1", artist["user"]["id"]) == 0
    # The other studio is untouched.
    assert [p["id"] for p in (await client.get("/api/v1/projects", headers=b["headers"])).json()] == [pb["id"]]
