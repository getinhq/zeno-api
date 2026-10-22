"""Multi-tenant isolation tests (real Postgres).

Run against a throwaway database, never the dev one:

    ZENO_TEST_DATABASE_URL=postgresql://.../zeno_test pytest tests/test_tenancy.py
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from tests.conftest import TEST_DB, make_project as _make_project, signup as _signup

pytestmark = pytest.mark.skipif(not TEST_DB, reason="ZENO_TEST_DATABASE_URL not set")


async def test_signup_returns_org_admin(client):
    a = await _signup(client, "Alpha")
    assert a["user"]["app_role"] == "pipeline"
    assert a["user"]["organization"]["name"].startswith("Alpha")
    me = await client.get("/api/v1/auth/me", headers=a["headers"])
    assert me.status_code == 200
    assert me.json()["organization"]["id"] == a["user"]["organization"]["id"]


async def test_login_with_email(client):
    a = await _signup(client, "Mailer")
    r = await client.post(
        "/api/v1/auth/login",
        json={"username": a["user"]["email"].upper(), "password": "correct-horse-battery"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["user"]["id"] == a["user"]["id"]


async def test_auth_required_but_config_public(client):
    assert (await client.get("/api/v1/projects")).status_code == 401
    cfg = await client.get("/api/v1/auth/config")
    assert cfg.status_code == 200
    assert cfg.json()["signup_enabled"] is True


async def test_signup_validation(client):
    r = await client.post(
        "/api/v1/auth/signup",
        json={"studio_name": "X", "email": "not-an-email", "password": "correct-horse"},
    )
    assert r.status_code == 400
    r = await client.post(
        "/api/v1/auth/signup",
        json={"studio_name": "X", "email": f"x-{uuid4().hex[:6]}@example.com", "password": "short"},
    )
    assert r.status_code == 400


async def test_projects_are_isolated_and_codes_are_per_org(client):
    a = await _signup(client, "Alpha")
    b = await _signup(client, "Beta")
    pa = await _make_project(client, a, code="SHARED")
    # Same code in another org is allowed.
    pb = await _make_project(client, b, code="SHARED")
    assert pa["id"] != pb["id"]

    listed_b = (await client.get("/api/v1/projects?status=all", headers=b["headers"])).json()
    assert [p["id"] for p in listed_b] == [pb["id"]]
    assert (await client.get(f"/api/v1/projects/{pa['id']}", headers=b["headers"])).status_code == 404
    r = await client.patch(
        f"/api/v1/projects/{pa['id']}", json={"name": "hijack"}, headers=b["headers"]
    )
    assert r.status_code == 404
    assert (await client.delete(f"/api/v1/projects/{pa['id']}", headers=b["headers"])).status_code == 404


async def test_entity_tree_is_isolated(client):
    a = await _signup(client, "Alpha")
    b = await _signup(client, "Beta")
    pa = await _make_project(client, a)
    ha, hb = a["headers"], b["headers"]

    ep = (await client.post(
        f"/api/v1/projects/{pa['id']}/episodes",
        json={"episode_number": 1, "code": "EP01", "title": "Pilot"},
        headers=ha,
    )).json()
    seq = (await client.post(
        f"/api/v1/episodes/{ep['id']}/sequences",
        json={"name": "Opening", "code": "SQ010"},
        headers=ha,
    )).json()
    shot = (await client.post(
        f"/api/v1/sequences/{seq['id']}/shots", json={"shot_code": "SH0010"}, headers=ha
    )).json()
    asset = (await client.post(
        f"/api/v1/projects/{pa['id']}/assets",
        json={"type": "character", "name": "Hero", "code": "hero"},
        headers=ha,
    )).json()
    task = (await client.post(
        "/api/v1/tasks",
        json={"type": "rig", "title": "Rig hero", "project_id": pa["id"], "asset_id": asset["id"]},
        headers=ha,
    )).json()

    for path in (
        f"/api/v1/episodes/{ep['id']}",
        f"/api/v1/sequences/{seq['id']}",
        f"/api/v1/shots/{shot['id']}",
        f"/api/v1/assets/{asset['id']}",
        f"/api/v1/tasks/{task['id']}",
        f"/api/v1/projects/{pa['id']}/assets",
        f"/api/v1/projects/{pa['id']}/shots",
        f"/api/v1/assets/{asset['id']}/versions",
    ):
        assert (await client.get(path, headers=hb)).status_code == 404, path
        assert (await client.get(path, headers=ha)).status_code == 200, path

    # Writes into another org's tree are refused.
    r = await client.post(
        f"/api/v1/projects/{pa['id']}/assets",
        json={"type": "prop", "name": "Mug", "code": "mug"},
        headers=hb,
    )
    assert r.status_code == 404
    r = await client.patch(f"/api/v1/tasks/{task['id']}", json={"status": "done"}, headers=hb)
    assert r.status_code == 404

    # Task lists never leak across orgs.
    tasks_b = (await client.get("/api/v1/tasks", headers=hb)).json()
    assert task["id"] not in {t["id"] for t in tasks_b}


async def test_cannot_reference_foreign_users_or_entities(client):
    a = await _signup(client, "Alpha")
    b = await _signup(client, "Beta")
    pa = await _make_project(client, a)
    pb = await _make_project(client, b)

    # Alpha cannot assign Beta's user.
    r = await client.post(
        "/api/v1/tasks",
        json={"type": "model", "project_id": pa["id"], "assignees": [b["user"]["id"]]},
        headers=a["headers"],
    )
    assert r.status_code == 404
    # Alpha cannot create a task inside Beta's project.
    r = await client.post(
        "/api/v1/tasks", json={"type": "model", "project_id": pb["id"]}, headers=a["headers"]
    )
    assert r.status_code == 404


async def test_user_directory_is_per_org(client):
    a = await _signup(client, "Alpha")
    b = await _signup(client, "Beta")
    ids_a = {u["id"] for u in (await client.get("/api/v1/users", headers=a["headers"])).json()}
    assert a["user"]["id"] in ids_a
    assert b["user"]["id"] not in ids_a
    assert (await client.get(f"/api/v1/users/{b['user']['id']}", headers=a["headers"])).status_code == 404
