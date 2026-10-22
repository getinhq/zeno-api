"""Shared test fixtures."""
import pytest

import app.db as app_db
import app.redis_conn as app_redis


@pytest.fixture(autouse=True)
def _fresh_connection_singletons():
    """
    The asyncpg pool and Redis client are process-wide singletons bound to the event loop
    that created them. Each TestClient / async test runs its own loop, so a singleton left
    over from an earlier test fails with "Event loop is closed". Start every test without one.
    """
    app_db._pool = None
    app_redis._redis = None
    yield
    app_db._pool = None
    app_redis._redis = None


# --- Real-database API fixtures (multi-tenant tests) ------------------------------------

import os  # noqa: E402
from uuid import uuid4  # noqa: E402

import httpx  # noqa: E402

import app.config as app_config  # noqa: E402

TEST_DB = os.environ.get("ZENO_TEST_DATABASE_URL", "").strip()
PASSWORD = "correct-horse-battery"


@pytest.fixture
async def client(monkeypatch):
    """Async client against the real app with auth on, backed by ZENO_TEST_DATABASE_URL."""
    if not TEST_DB:
        pytest.skip("ZENO_TEST_DATABASE_URL not set")
    monkeypatch.setattr(app_config, "DATABASE_URL", TEST_DB)
    monkeypatch.setattr(app_config, "ENABLE_AUTH", True)
    monkeypatch.setattr(app_config, "ALLOW_SIGNUP", True)
    monkeypatch.setattr(app_config, "SMTP_HOST", None)
    from app.db import close_pool
    from main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await close_pool()


def auth(data: dict) -> dict:
    return {"Authorization": f"Bearer {data['access_token']}"}


async def signup(client, studio: str) -> dict:
    tag = uuid4().hex[:8]
    r = await client.post(
        "/api/v1/auth/signup",
        json={
            "studio_name": f"{studio} {tag}",
            "name": f"{studio} Owner",
            "email": f"{studio.lower()}-{tag}@example.com",
            "password": PASSWORD,
        },
    )
    assert r.status_code == 201, r.text
    data = r.json()
    data["headers"] = auth(data)
    return data


async def make_project(client, owner, code="SHORT01", **extra) -> dict:
    r = await client.post(
        "/api/v1/projects",
        json={"name": f"Film {uuid4().hex[:6]}", "code": code, **extra},
        headers=owner["headers"],
    )
    assert r.status_code in (200, 201), r.text
    return r.json()


async def invite_and_accept(client, admin, role="artist", **extra) -> dict:
    """Invite someone into ``admin``'s studio and accept it; returns their session."""
    inv = await client.post(
        "/api/v1/invites", json={"app_role": role, **extra}, headers=admin["headers"]
    )
    assert inv.status_code == 201, inv.text
    token = inv.json()["link"].rsplit("/", 1)[1]
    email = extra.get("email") or f"{role}-{uuid4().hex[:8]}@example.com"
    r = await client.post(
        f"/api/v1/invites/public/{token}/accept",
        json={"name": f"New {role}", "email": email, "password": PASSWORD},
    )
    assert r.status_code == 201, r.text
    data = r.json()
    data["headers"] = auth(data)
    return data
