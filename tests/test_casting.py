"""Shot casting / breakdown."""
from __future__ import annotations

import pytest

from tests.conftest import TEST_DB, invite_and_accept, signup

pytestmark = pytest.mark.skipif(not TEST_DB, reason="ZENO_TEST_DATABASE_URL not set")


async def _film(client, owner, code="FILM", assets=None):
    r = await client.post(
        "/api/v1/projects/from-template",
        json={
            "template": "short_film",
            "name": f"Film {code}",
            "code": code,
            "structure": {"episodes": 1, "sequences_per_episode": 2, "shots_per_sequence": 2},
            "assets": assets or {"character": ["Nono", "Grandma"], "prop": ["Mug"]},
        },
        headers=owner["headers"],
    )
    assert r.status_code == 201, r.text
    pid = r.json()["project"]["id"]
    bd = (await client.get(f"/api/v1/projects/{pid}/breakdown", headers=owner["headers"])).json()
    return pid, bd


async def test_cast_replace_remove_and_appears_in(client):
    a = await signup(client, "Alpha")
    h = a["headers"]
    pid, bd = await _film(client, a)
    assert [s["code"] for s in bd["shots"]] == ["SH0010", "SH0020", "SH0010", "SH0020"]
    nono = next(x for x in bd["assets"] if x["code"] == "nono")
    mug = next(x for x in bd["assets"] if x["code"] == "mug")
    shot = bd["shots"][0]

    r = await client.post(f"/api/v1/shots/{shot['id']}/casting", json={"asset_id": nono["id"]}, headers=h)
    assert r.status_code == 201
    assert [c["asset"]["code"] for c in r.json()] == ["nono"]
    assert r.json()[0]["role"] == "character"  # defaults to the asset type
    # Casting the same asset twice is a no-op; a named second instance is allowed.
    await client.post(f"/api/v1/shots/{shot['id']}/casting", json={"asset_id": nono["id"]}, headers=h)
    r = await client.post(
        f"/api/v1/shots/{shot['id']}/casting", json={"asset_id": nono["id"], "instance_name": "nono_dream"}, headers=h
    )
    assert len(r.json()) == 2

    r = await client.put(
        f"/api/v1/shots/{shot['id']}/casting",
        json={"items": [{"asset_id": mug["id"], "role": "hero prop"}, {"asset_id": nono["id"]}]},
        headers=h,
    )
    cast = r.json()
    assert [(c["asset"]["code"], c["role"]) for c in cast] == [("mug", "hero prop"), ("nono", "character")]

    appears = (await client.get(f"/api/v1/assets/{mug['id']}/shots", headers=h)).json()
    assert [x["shot"]["code"] for x in appears] == ["SH0010"]

    assert (await client.delete(f"/api/v1/casting/{cast[0]['id']}", headers=h)).status_code == 200
    assert len((await client.get(f"/api/v1/shots/{shot['id']}/casting", headers=h)).json()) == 1


async def test_bulk_breakdown(client):
    a = await signup(client, "Alpha")
    h = a["headers"]
    pid, bd = await _film(client, a)
    nono = next(x for x in bd["assets"] if x["code"] == "nono")
    adds = [{"shot_id": s["id"], "asset_id": nono["id"]} for s in bd["shots"]]
    r = await client.post(f"/api/v1/projects/{pid}/breakdown", json={"add": adds}, headers=h)
    assert r.json() == {"added": 4, "removed": 0}
    r = await client.post(f"/api/v1/projects/{pid}/breakdown", json={"remove": adds[:1]}, headers=h)
    links = (await client.get(f"/api/v1/projects/{pid}/breakdown", headers=h)).json()["links"]
    assert len(links) == 3

    bad = await client.post(f"/api/v1/projects/{pid}/breakdown", json={"add": [{"shot_id": "nope"}]}, headers=h)
    assert bad.status_code == 400


async def test_cannot_cast_across_projects_or_orgs(client):
    a = await signup(client, "Alpha")
    b = await signup(client, "Beta")
    pid1, bd1 = await _film(client, a, code="ONE")
    pid2, bd2 = await _film(client, a, code="TWO")
    other_asset = bd2["assets"][0]
    shot = bd1["shots"][0]
    r = await client.post(f"/api/v1/shots/{shot['id']}/casting", json={"asset_id": other_asset["id"]}, headers=a["headers"])
    assert r.status_code == 400
    r = await client.post(
        f"/api/v1/projects/{pid1}/breakdown",
        json={"add": [{"shot_id": shot["id"], "asset_id": other_asset["id"]}]},
        headers=a["headers"],
    )
    assert r.status_code == 400

    # Another studio sees nothing.
    assert (await client.get(f"/api/v1/projects/{pid1}/breakdown", headers=b["headers"])).status_code == 404
    assert (await client.get(f"/api/v1/shots/{shot['id']}/casting", headers=b["headers"])).status_code == 404


async def test_artists_read_only(client):
    a = await signup(client, "Alpha")
    artist = await invite_and_accept(client, a)
    pid, bd = await _film(client, a)
    shot, asset = bd["shots"][0], bd["assets"][0]
    assert (await client.get(f"/api/v1/projects/{pid}/breakdown", headers=artist["headers"])).status_code == 200
    r = await client.post(f"/api/v1/shots/{shot['id']}/casting", json={"asset_id": asset["id"]}, headers=artist["headers"])
    assert r.status_code == 403
