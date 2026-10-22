"""Studio storage workflow: defaults, switches, per-extension rules, permissions, enforcement."""
from __future__ import annotations

import pytest

from app.storage_policy import policy
from tests.conftest import TEST_DB, invite_and_accept, signup


def test_resolution_rules():
    assert policy.resolve({}, "shot.blend")["strategy"] == "chunked"
    assert policy.resolve({}, ".EXR")["strategy"] == "per_frame"
    assert policy.resolve({}, "cut.mov")["strategy"] == "whole"
    assert policy.resolve({}, "thing.hda") == {
        "extension": "hda", "kind": "scene", "strategy": "chunked", "source": "default",
        "reason": "Default for file types without a rule.",
    }
    stored = {"rules": {"fbx": "whole", "exr": "off"}, "default_scene_strategy": "whole"}
    assert policy.resolve(stored, "a.fbx")["source"] == "rule"
    assert policy.resolve(stored, "a.fbx")["strategy"] == "whole"
    assert policy.resolve(stored, "a.exr")["strategy"] == "off"
    assert policy.resolve(stored, "a.hda")["strategy"] == "whole"
    off = {**stored, "chunking_enabled": False, "rules": {"blend": "chunked"}}
    assert policy.resolve(off, "a.blend")["strategy"] == "whole"
    assert policy.resolve(off, "a.png")["strategy"] == "off"
    assert policy.resolve(off, "a.mp4")["strategy"] == "whole"


@pytest.mark.skipif(not TEST_DB, reason="ZENO_TEST_DATABASE_URL not set")
async def test_studio_policy_api(client):
    owner = await signup(client, "Alpha")  # pipeline role
    artist = await invite_and_accept(client, owner, role="artist")
    other = await signup(client, "Beta")

    data = (await client.get("/api/v1/storage-policy", headers=artist["headers"])).json()
    assert data["chunking_enabled"] is True and data["algorithm_version"]
    assert data["effective"]["blend"]["strategy"] == "chunked"
    assert {c["extension"] for c in data["catalog"]} >= {"blend", "ma", "usd", "exr", "mp4"}

    # Artists read, only pipeline writes.
    assert (await client.put("/api/v1/storage-policy", json={"chunking_enabled": False}, headers=artist["headers"])).status_code == 403
    r = await client.put(
        "/api/v1/storage-policy",
        json={"rules": {".FBX": "whole", "exr": "off", "hda": "whole"}, "default_scene_strategy": "whole"},
        headers=owner["headers"],
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["rules"] == {"fbx": "whole", "exr": "off", "hda": "whole"}
    assert data["effective"]["hda"]["source"] == "rule" and data["updated_by"]
    assert (await client.get("/api/v1/storage-policy/resolve?filename=shot.fbx", headers=artist["headers"])).json()["strategy"] == "whole"

    # Bad input is explained.
    bad = await client.put("/api/v1/storage-policy", json={"rules": {"exr": "chunked"}}, headers=owner["headers"])
    assert bad.status_code == 400 and "per_frame" in bad.json()["detail"]
    assert (await client.put("/api/v1/storage-policy", json={"rules": {"../x": "whole"}}, headers=owner["headers"])).status_code == 400

    # Studios are independent.
    assert (await client.get("/api/v1/storage-policy", headers=other["headers"])).json()["rules"] == {}

    # Master switch, then reset.
    data = (await client.put("/api/v1/storage-policy", json={"chunking_enabled": False}, headers=owner["headers"])).json()
    assert data["effective"]["blend"]["strategy"] == "whole" and data["rules"]["fbx"] == "whole"
    data = (await client.post("/api/v1/storage-policy/reset", headers=owner["headers"])).json()
    assert data["chunking_enabled"] is True and data["rules"] == {}
