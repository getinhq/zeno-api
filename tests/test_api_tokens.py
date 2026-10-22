"""Personal access tokens and DCC launch tokens."""
from __future__ import annotations

import pytest

from tests.conftest import TEST_DB, make_project, signup

pytestmark = pytest.mark.skipif(not TEST_DB, reason="ZENO_TEST_DATABASE_URL not set")


async def test_pat_lifecycle(client):
    a = await signup(client, "Alpha")
    r = await client.post("/api/v1/auth/tokens", json={"name": "Blender laptop", "expires_in_days": 30}, headers=a["headers"])
    assert r.status_code == 201
    created = r.json()
    token = created["token"]
    assert token.startswith("zeno_pat_") and created["prefix"] == token[:15]

    pat = {"Authorization": f"Bearer {token}"}
    me = await client.get("/api/v1/auth/me", headers=pat)
    assert me.status_code == 200 and me.json()["id"] == a["user"]["id"]
    # The token is scoped to the user's studio like a normal session.
    await make_project(client, a, code="PAT1")
    assert [p["code"] for p in (await client.get("/api/v1/projects", headers=pat)).json()] == ["PAT1"]

    listed = (await client.get("/api/v1/auth/tokens", headers=a["headers"])).json()
    assert listed[0]["name"] == "Blender laptop" and "token" not in listed[0]
    assert listed[0]["last_used_at"]

    assert (await client.delete(f"/api/v1/auth/tokens/{created['id']}", headers=a["headers"])).status_code == 200
    assert (await client.get("/api/v1/auth/me", headers=pat)).status_code == 401
    assert (await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer zeno_pat_nope"})).status_code == 401


async def test_tokens_are_per_user(client):
    a = await signup(client, "Alpha")
    b = await signup(client, "Beta")
    t = (await client.post("/api/v1/auth/tokens", json={"name": "x"}, headers=a["headers"])).json()
    assert (await client.delete(f"/api/v1/auth/tokens/{t['id']}", headers=b["headers"])).status_code == 404


async def test_launch_context_carries_a_user_token(client, monkeypatch):
    import app.config as cfg

    monkeypatch.setattr(cfg, "REDIS_URL", "redis://redis:6379/15")
    a = await signup(client, "Alpha")
    p = await make_project(client, a, code="LAUNCH")
    ctx = {"version": "1", "intent": "open_project", "project_id": p["id"], "dcc": "blender",
           "access_token": "zeno_pat_forged"}
    minted = await client.post("/api/v1/launch-tokens", json={"context": ctx}, headers=a["headers"])
    assert minted.status_code == 200, minted.text
    exchanged = (await client.get(f"/api/v1/launch-tokens/{minted.json()['token']}")).json()["context"]
    dcc_token = exchanged["access_token"]
    assert dcc_token != "zeno_pat_forged"
    me = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {dcc_token}"})
    assert me.json()["id"] == a["user"]["id"]
