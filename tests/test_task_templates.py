"""Task templates: defaults, auto-generation on create, apply, validation, isolation."""
from __future__ import annotations

import pytest

from tests.conftest import TEST_DB, invite_and_accept, make_project, signup

pytestmark = pytest.mark.skipif(not TEST_DB, reason="ZENO_TEST_DATABASE_URL not set")


async def _shot(client, owner, project_id):
    h = owner["headers"]
    ep = (await client.post(f"/api/v1/projects/{project_id}/episodes", json={"episode_number": 1, "code": "EP01"}, headers=h)).json()
    seq = (await client.post(f"/api/v1/episodes/{ep['id']}/sequences", json={"name": "Open", "code": "SQ010"}, headers=h)).json()
    r = await client.post(f"/api/v1/sequences/{seq['id']}/shots", json={"shot_code": "SH0010"}, headers=h)
    assert r.status_code in (200, 201), r.text
    return r.json()


async def test_defaults_are_seeded_per_studio(client):
    a = await signup(client, "Alpha")
    templates = (await client.get("/api/v1/task-templates", headers=a["headers"])).json()
    by_type = {(t["entity_kind"], t["asset_type"]): t for t in templates}
    character = by_type[("asset", "character")]
    assert character["is_default"]
    assert [s["type"] for s in character["steps"]] == ["concept", "model", "texture", "rig", "lookdev"]
    assert character["steps"][3]["depends_on"] == [2]
    assert ("shot", None) in by_type


async def test_new_asset_and_shot_get_template_tasks(client):
    a = await signup(client, "Alpha")
    p = await make_project(client, a)
    r = await client.post(
        f"/api/v1/projects/{p['id']}/assets",
        json={"type": "character", "name": "Nono", "code": "nono"},
        headers=a["headers"],
    )
    asset = r.json()
    assert asset["tasks_created"] == 5
    tasks = (await client.get(f"/api/v1/tasks?asset_id={asset['id']}", headers=a["headers"])).json()
    assert {t["type"] for t in tasks} == {"concept", "model", "texture", "rig", "lookdev"}
    rig = next(t for t in tasks if t["type"] == "rig")
    assert rig["title"] == "Rig · Nono"
    assert rig["project_id"] == p["id"]

    shot = await _shot(client, a, p["id"])
    assert shot["tasks_created"] == 5

    # Opting out works.
    r = await client.post(
        f"/api/v1/projects/{p['id']}/assets",
        json={"type": "prop", "name": "Mug", "code": "mug", "apply_template": False},
        headers=a["headers"],
    )
    assert r.json()["tasks_created"] == 0


async def test_apply_is_idempotent_and_custom_default_wins(client):
    a = await signup(client, "Alpha")
    h = a["headers"]
    p = await make_project(client, a)
    asset = (await client.post(
        f"/api/v1/projects/{p['id']}/assets",
        json={"type": "prop", "name": "Lamp", "code": "lamp", "apply_template": False},
        headers=h,
    )).json()

    custom = await client.post(
        "/api/v1/task-templates",
        json={
            "name": "Hero prop",
            "entity_kind": "asset",
            "asset_type": "prop",
            "is_default": True,
            "steps": [
                {"type": "model", "title": "Sculpt", "duration_days": 3},
                {"type": "rig", "title": "Rig", "duration_days": 1, "depends_on": [0]},
            ],
        },
        headers=h,
    )
    assert custom.status_code == 201, custom.text
    custom = custom.json()
    # The built-in prop template is no longer the default.
    defaults = [t for t in (await client.get("/api/v1/task-templates", headers=h)).json()
                if t["asset_type"] == "prop" and t["is_default"]]
    assert [t["id"] for t in defaults] == [custom["id"]]

    r1 = await client.post(f"/api/v1/task-templates/{custom['id']}/apply", json={"asset_ids": [asset["id"]]}, headers=h)
    r2 = await client.post(f"/api/v1/task-templates/{custom['id']}/apply", json={"asset_ids": [asset["id"]]}, headers=h)
    assert r1.json()["tasks_created"] == 2
    assert r2.json()["tasks_created"] == 0

    new = (await client.post(
        f"/api/v1/projects/{p['id']}/assets", json={"type": "prop", "name": "Chair", "code": "chair"}, headers=h
    )).json()
    assert new["tasks_created"] == 2


async def test_validation(client):
    a = await signup(client, "Alpha")
    bad_steps = [
        [],
        [{"type": "juggling"}],
        [{"type": "model", "depends_on": [0]}],
        [{"type": "model", "duration_days": -1}],
    ]
    for steps in bad_steps:
        r = await client.post(
            "/api/v1/task-templates",
            json={"name": "X", "entity_kind": "asset", "asset_type": "prop", "steps": steps},
            headers=a["headers"],
        )
        assert r.status_code == 400, steps


async def test_templates_are_isolated_and_managed_by_management(client):
    a = await signup(client, "Alpha")
    b = await signup(client, "Beta")
    t_a = (await client.get("/api/v1/task-templates", headers=a["headers"])).json()[0]
    ids_b = {t["id"] for t in (await client.get("/api/v1/task-templates", headers=b["headers"])).json()}
    assert t_a["id"] not in ids_b
    assert (await client.patch(f"/api/v1/task-templates/{t_a['id']}", json={"name": "x"}, headers=b["headers"])).status_code == 404
    assert (await client.delete(f"/api/v1/task-templates/{t_a['id']}", headers=b["headers"])).status_code == 404

    artist = await invite_and_accept(client, a)
    r = await client.patch(f"/api/v1/task-templates/{t_a['id']}", json={"name": "x"}, headers=artist["headers"])
    assert r.status_code == 403
