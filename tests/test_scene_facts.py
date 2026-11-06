"""Scene facts: what a DCC reports a shot file contains, compared with approvals and casting."""
from __future__ import annotations

import pytest
from blake3 import blake3

from app.scene_facts.report import asset_state, build_report
from tests.conftest import TEST_DB, signup


def test_asset_state():
    assert asset_state(5, 5) == "current"
    assert asset_state(3, 5) == "outdated"
    assert asset_state(6, 5) == "ahead"
    assert asset_state(1, None) == "unapproved"


def test_build_report_pure():
    v = lambda aid, code, n: {"asset_id": aid, "asset_code": code, "asset_name": code.title(), "representation": "blend",  # noqa: E731
                              "pipeline_stage": "rigging", "version_id": f"{code}{n}", "version_number": n}
    facts = {"assets": [{"content_id": "c1", "library": "//nono.blend"}, {"content_id": "zz", "library": "/nas/tree.blend"},
                        {"content_id": "c2", "library": "//mug.blend"}]}
    report = build_report(
        facts,
        versions_by_cid={"c1": v("A", "nono", 3), "c2": v("B", "mug", 1)},
        approved_latest={("A", "blend", "rigging"): 5},
        casting=[{"asset_id": "A", "code": "nono", "name": "Nono"}, {"asset_id": "C", "code": "grandma", "name": "Grandma"}],
    )
    states = [(r.get("code"), r["state"]) for r in report["assets"]]
    assert states == [("nono", "outdated"), (None, "unknown"), ("mug", "unapproved")]
    assert report["assets"][0]["approved_version"] == 5
    assert [x["code"] for x in report["not_cast"]] == ["mug"]
    assert [x["code"] for x in report["missing"]] == ["grandma"]
    assert report["counts"]["outdated"] == 1 and report["counts"]["unknown"] == 1


pytestmark_db = pytest.mark.skipif(not TEST_DB, reason="ZENO_TEST_DATABASE_URL not set")


@pytest.fixture
def cas_root(tmp_path, monkeypatch):
    from app import config

    root = tmp_path / "cas"
    root.mkdir()
    monkeypatch.setattr(config, "CAS_ROOT", str(root))
    monkeypatch.setattr(config, "CAS_STORAGE_BACKEND", "nas")
    monkeypatch.setattr(config, "MONGO_URI", None)
    return root


async def _film(client, owner, code):
    r = await client.post(
        "/api/v1/projects/from-template",
        json={"template": "short_film", "name": f"Film {code}", "code": code,
              "structure": {"episodes": 1, "sequences_per_episode": 1, "shots_per_sequence": 1},
              "assets": {"character": ["Nono", "Grandma"], "prop": ["Mug"]}},
        headers=owner["headers"],
    )
    assert r.status_code == 201, r.text
    pid = r.json()["project"]["id"]
    return (await client.get(f"/api/v1/projects/{pid}/breakdown", headers=owner["headers"])).json()


async def _version(client, who, project, asset, payload: bytes, *, approve=False):
    cid = blake3(payload).hexdigest()
    await client.put(f"/api/v1/cas/blobs/{cid}", content=payload, headers=who["headers"])
    body = {"project": project, "asset": asset, "representation": "blend", "version": "next",
            "content_id": cid, "filename": f"{asset}.blend", "size": len(payload)}
    r = await client.post("/api/v1/versions", json=body, headers=who["headers"])
    assert r.status_code == 201, r.text
    v = r.json()
    if approve:
        r = await client.patch(f"/api/v1/versions/{v['version_id']}", json={"status": "approved"}, headers=who["headers"])
        assert r.status_code == 200, r.text
    return cid


@pytestmark_db
async def test_scene_facts_report_and_scope(client, cas_root):
    a = await signup(client, "Alpha")
    b = await signup(client, "Beta")
    h = a["headers"]
    bd = await _film(client, a, "FACTS")
    shot = bd["shots"][0]
    ids = {x["code"]: x["id"] for x in bd["assets"]}
    r = await client.patch(f"/api/v1/shots/{shot['id']}", json={"frame_start": 1001, "frame_end": 1024}, headers=h)
    assert r.status_code == 200, r.text
    await client.put(f"/api/v1/shots/{shot['id']}/casting",
                     json={"items": [{"asset_id": ids["nono"]}, {"asset_id": ids["grandma"]}]}, headers=h)

    nono_v1 = await _version(client, a, "FACTS", "nono", b"nono v1", approve=True)
    await _version(client, a, "FACTS", "nono", b"nono v2", approve=True)
    nono_v3 = await _version(client, a, "FACTS", "nono", b"nono v3")
    mug_v1 = await _version(client, a, "FACTS", "mug", b"mug v1")

    facts = {"dcc": "blender", "file": "sh0010_anim.blend", "frame_start": 1001, "frame_end": 1024, "fps": "24",
             "camera": "CAM_sh0010", "assets": [
                 {"content_id": nono_v1, "library": "//libs/nono.blend"},
                 {"content_id": mug_v1, "library": "//libs/mug.blend"},
                 {"content_id": "f" * 64, "library": "/nas/old/tree.blend"}]}
    r = await client.put(f"/api/v1/shots/{shot['id']}/scene-facts", json=facts, headers=h)
    assert r.status_code == 200, r.text
    report = r.json()["report"]
    rows = {x.get("code") or x["library"]: x for x in report["assets"]}
    assert (rows["nono"]["version"], rows["nono"]["approved_version"], rows["nono"]["state"]) == (1, 2, "outdated")
    assert rows["mug"]["state"] == "unapproved"
    assert rows["/nas/old/tree.blend"]["state"] == "unknown"
    assert [x["code"] for x in report["not_cast"]] == ["mug"]
    assert [x["code"] for x in report["missing"]] == ["grandma"]
    assert report["range_matches"] is True and report["shot"]["code"] == shot["code"]

    # Newer than approved is "ahead"; the stored facts come back on GET.
    facts["assets"] = [{"content_id": nono_v3, "library": "//libs/nono.blend"}]
    await client.put(f"/api/v1/shots/{shot['id']}/scene-facts", json=facts, headers=h)
    got = (await client.get(f"/api/v1/shots/{shot['id']}/scene-facts", headers=h)).json()
    assert got["facts"]["camera"] == "CAM_sh0010" and got["facts"]["reported_by"]
    assert [(x["code"], x["state"]) for x in got["report"]["assets"]] == [("nono", "ahead")]

    # Lookup by content id, scoped to the caller's studio.
    found = (await client.post("/api/v1/versions/lookup", json={"content_ids": [nono_v1, "0" * 64]}, headers=h)).json()
    assert [(x["asset_code"], x["version"], x["status"]) for x in found] == [("nono", 1, "approved")]
    assert (await client.post("/api/v1/versions/lookup", json={"content_ids": [nono_v1]}, headers=b["headers"])).json() == []
    assert (await client.get(f"/api/v1/shots/{shot['id']}/scene-facts", headers=b["headers"])).status_code == 404
    assert (await client.put(f"/api/v1/shots/{shot['id']}/scene-facts", json=facts, headers=b["headers"])).status_code == 404
