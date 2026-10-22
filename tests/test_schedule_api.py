"""Schedule API: template + casting dependencies, auto-schedule, manual links, milestones."""
from __future__ import annotations

from datetime import date

import pytest

from tests.conftest import TEST_DB, invite_and_accept, signup

pytestmark = pytest.mark.skipif(not TEST_DB, reason="ZENO_TEST_DATABASE_URL not set")


async def _film(client, owner, end="2027-03-31"):
    r = await client.post(
        "/api/v1/projects/from-template",
        json={
            "template": "short_film",
            "name": "Sched",
            "code": "SCHED",
            "start_date": "2027-01-04",
            "end_date": end,
            "structure": {"episodes": 1, "sequences_per_episode": 1, "shots_per_sequence": 2},
            "assets": {"character": ["Nono"]},
        },
        headers=owner["headers"],
    )
    assert r.status_code == 201, r.text
    return r.json()["project"]["id"]


def _by(tasks, group_label, type_):
    return next(t for t in tasks if t["group"]["label"] == group_label and t["type"] == type_)


async def test_template_dependencies_and_auto_schedule_with_casting(client):
    a = await signup(client, "Alpha")
    h = a["headers"]
    pid = await _film(client, a)
    sched = (await client.get(f"/api/v1/projects/{pid}/schedule", headers=h)).json()
    tasks = sched["tasks"]
    deps = {(d["task_id"], d["depends_on"]) for d in sched["dependencies"]}
    rig = _by(tasks, "Nono", "rig")
    texture = _by(tasks, "Nono", "texture")
    assert (rig["id"], texture["id"]) in deps  # from the character template
    assert len(sched["milestones"]) == 5

    # Cast Nono into SH0010 only, then auto-schedule.
    bd = (await client.get(f"/api/v1/projects/{pid}/breakdown", headers=h)).json()
    sh10 = next(s for s in bd["shots"] if s["code"] == "SH0010")
    nono = bd["assets"][0]
    await client.post(f"/api/v1/projects/{pid}/breakdown", json={"add": [{"shot_id": sh10["id"], "asset_id": nono["id"]}]}, headers=h)

    res = await client.post(f"/api/v1/projects/{pid}/schedule/auto", json={}, headers=h)
    assert res.status_code == 200, res.text
    summary = res.json()
    assert summary["scheduled"] == len(tasks)
    assert summary["casting_links_created"] >= 1
    assert summary["late_by_days"] == 0

    tasks = (await client.get(f"/api/v1/projects/{pid}/schedule", headers=h)).json()["tasks"]
    rig = _by(tasks, "Nono", "rig")
    anim10 = _by(tasks, "SQ010 / SH0010", "animation")
    anim20 = _by(tasks, "SQ010 / SH0020", "animation")
    assert all(t["start_date"] and t["due_date"] for t in tasks)
    assert date.fromisoformat(anim10["start_date"]) > date.fromisoformat(rig["due_date"])
    # SH0020 has no cast character, so its animation does not wait for the rig.
    assert date.fromisoformat(anim20["start_date"]) < date.fromisoformat(rig["due_date"])
    for t in tasks:
        assert date.fromisoformat(t["start_date"]).weekday() < 5


async def test_reports_lateness(client):
    a = await signup(client, "Alpha")
    pid = await _film(client, a, end="2027-01-08")
    summary = (await client.post(f"/api/v1/projects/{pid}/schedule/auto", json={}, headers=a["headers"])).json()
    assert summary["late_by_days"] > 0


async def test_manual_dependencies_and_cycles(client):
    a = await signup(client, "Alpha")
    h = a["headers"]
    pid = await _film(client, a)
    tasks = (await client.get(f"/api/v1/projects/{pid}/schedule", headers=h)).json()["tasks"]
    concept = _by(tasks, "Nono", "concept")
    lookdev = _by(tasks, "Nono", "lookdev")
    # concept -> lookdev would close the template chain into a loop.
    r = await client.post(f"/api/v1/tasks/{concept['id']}/dependencies", json={"depends_on": lookdev["id"]}, headers=h)
    assert r.status_code == 409
    layout = _by(tasks, "SQ010 / SH0010", "layout")
    r = await client.post(f"/api/v1/tasks/{layout['id']}/dependencies", json={"depends_on": concept["id"]}, headers=h)
    assert r.status_code == 201
    r = await client.delete(f"/api/v1/tasks/{layout['id']}/dependencies/{concept['id']}", headers=h)
    assert r.status_code == 200
    r = await client.post(f"/api/v1/tasks/{layout['id']}/dependencies", json={"depends_on": layout["id"]}, headers=h)
    assert r.status_code == 400


async def test_move_bar_and_milestones(client):
    a = await signup(client, "Alpha")
    h = a["headers"]
    pid = await _film(client, a)
    task = (await client.get(f"/api/v1/projects/{pid}/schedule", headers=h)).json()["tasks"][0]
    r = await client.patch(f"/api/v1/tasks/{task['id']}/dates", json={"start_date": "2027-02-01", "due_date": "2027-02-03"}, headers=h)
    assert r.json() == {"id": task["id"], "start_date": "2027-02-01", "due_date": "2027-02-03"}
    bad = await client.patch(f"/api/v1/tasks/{task['id']}/dates", json={"start_date": "2027-02-05", "due_date": "2027-02-03"}, headers=h)
    assert bad.status_code == 400
    # The regular task endpoint reports the start date too.
    assert (await client.get(f"/api/v1/tasks/{task['id']}", headers=h)).json()["start_date"] == "2027-02-01"

    m = await client.post(f"/api/v1/projects/{pid}/milestones", json={"name": "Client review", "due_date": "2027-02-15", "kind": "review"}, headers=h)
    assert m.status_code == 201
    mid = m.json()["id"]
    upd = await client.patch(f"/api/v1/milestones/{mid}", json={"done": True, "due_date": "2027-02-16"}, headers=h)
    assert upd.json()["done"] and upd.json()["due_date"] == "2027-02-16"
    assert (await client.delete(f"/api/v1/milestones/{mid}", headers=h)).status_code == 200


async def test_schedule_isolation_and_roles(client):
    a = await signup(client, "Alpha")
    b = await signup(client, "Beta")
    pid = await _film(client, a)
    assert (await client.get(f"/api/v1/projects/{pid}/schedule", headers=b["headers"])).status_code == 404
    assert (await client.post(f"/api/v1/projects/{pid}/schedule/auto", json={}, headers=b["headers"])).status_code == 404
    artist = await invite_and_accept(client, a)
    assert (await client.get(f"/api/v1/projects/{pid}/schedule", headers=artist["headers"])).status_code == 200
    assert (await client.post(f"/api/v1/projects/{pid}/schedule/auto", json={}, headers=artist["headers"])).status_code == 403
