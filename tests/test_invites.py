"""Invite links: create, preview, accept, revoke, regenerate, isolation."""
from __future__ import annotations

from uuid import uuid4

import pytest

from tests.conftest import PASSWORD, TEST_DB, invite_and_accept, make_project, signup

pytestmark = pytest.mark.skipif(not TEST_DB, reason="ZENO_TEST_DATABASE_URL not set")


def _token(invite: dict) -> str:
    return invite["link"].rsplit("/invite/", 1)[1]


async def test_invite_link_flow(client):
    admin = await signup(client, "Studio")
    email = f"artist-{uuid4().hex[:8]}@example.com"
    r = await client.post(
        "/api/v1/invites",
        json={"email": email, "app_role": "artist", "message": "Welcome aboard"},
        headers=admin["headers"],
    )
    assert r.status_code == 201, r.text
    invite = r.json()
    assert invite["status"] == "pending"
    assert invite["email_sent"] is False and invite["email_configured"] is False
    assert invite["link"].startswith("http")

    preview = await client.get(f"/api/v1/invites/public/{_token(invite)}")
    assert preview.status_code == 200
    assert preview.json()["organization"] == admin["user"]["organization"]["name"]
    assert preview.json()["email"] == email

    accepted = await client.post(
        f"/api/v1/invites/public/{_token(invite)}/accept",
        json={"name": "Ana", "password": PASSWORD},
    )
    assert accepted.status_code == 201, accepted.text
    user = accepted.json()["user"]
    assert user["email"] == email
    assert user["app_role"] == "artist"
    assert user["organization"]["id"] == admin["user"]["organization"]["id"]

    # Same studio directory now includes the new artist.
    members = (await client.get("/api/v1/users", headers=admin["headers"])).json()
    assert user["id"] in {m["id"] for m in members}

    # The link is single-use.
    again = await client.post(
        f"/api/v1/invites/public/{_token(invite)}/accept",
        json={"name": "Ana 2", "email": "x@example.com", "password": PASSWORD},
    )
    assert again.status_code == 410

    listed = (await client.get("/api/v1/invites", headers=admin["headers"])).json()
    assert listed[0]["status"] == "accepted"


async def test_open_invite_requires_email_on_accept(client):
    admin = await signup(client, "Studio")
    inv = (await client.post("/api/v1/invites", json={"app_role": "supervisor"}, headers=admin["headers"])).json()
    r = await client.post(
        f"/api/v1/invites/public/{_token(inv)}/accept", json={"name": "S", "password": PASSWORD}
    )
    assert r.status_code == 400
    r = await client.post(
        f"/api/v1/invites/public/{_token(inv)}/accept",
        json={"name": "S", "email": f"s-{uuid4().hex[:6]}@example.com", "password": PASSWORD},
    )
    assert r.status_code == 201
    assert r.json()["user"]["app_role"] == "supervisor"


async def test_revoke_and_regenerate(client):
    admin = await signup(client, "Studio")
    inv = (await client.post("/api/v1/invites", json={}, headers=admin["headers"])).json()
    old = _token(inv)

    regen = await client.post(f"/api/v1/invites/{inv['id']}/regenerate", headers=admin["headers"])
    assert regen.status_code == 200
    new = _token(regen.json())
    assert new != old
    assert (await client.get(f"/api/v1/invites/public/{old}")).status_code == 404
    assert (await client.get(f"/api/v1/invites/public/{new}")).status_code == 200

    assert (await client.delete(f"/api/v1/invites/{inv['id']}", headers=admin["headers"])).status_code == 200
    assert (await client.get(f"/api/v1/invites/public/{new}")).status_code == 410


async def test_invite_permissions_and_isolation(client):
    a = await signup(client, "Alpha")
    b = await signup(client, "Beta")
    artist = await invite_and_accept(client, a, role="artist")

    # Artists cannot invite.
    r = await client.post("/api/v1/invites", json={}, headers=artist["headers"])
    assert r.status_code == 403

    inv = (await client.post("/api/v1/invites", json={}, headers=a["headers"])).json()
    # Another studio cannot see or revoke it.
    assert inv["id"] not in {i["id"] for i in (await client.get("/api/v1/invites", headers=b["headers"])).json()}
    assert (await client.delete(f"/api/v1/invites/{inv['id']}", headers=b["headers"])).status_code == 404
    # Nor invite into a project it does not own.
    pa = await make_project(client, a)
    r = await client.post("/api/v1/invites", json={"project_id": pa["id"]}, headers=b["headers"])
    assert r.status_code == 404


async def test_invite_existing_email_conflicts(client):
    a = await signup(client, "Alpha")
    r = await client.post(
        "/api/v1/invites", json={"email": a["user"]["email"]}, headers=a["headers"]
    )
    assert r.status_code == 409


async def test_organization_endpoint(client):
    a = await signup(client, "Alpha")
    await invite_and_accept(client, a)
    org = (await client.get("/api/v1/organization", headers=a["headers"])).json()
    assert org["members"] == 2
    renamed = await client.patch("/api/v1/organization", json={"name": "Renamed"}, headers=a["headers"])
    assert renamed.status_code == 200 and renamed.json()["name"] == "Renamed"
