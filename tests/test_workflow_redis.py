"""Integration-style tests for Redis-backed presence and locks."""
import os

import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.fixture
def client_with_redis(monkeypatch):
    """Configure REDIS_URL and return a TestClient."""
    # Default to local Redis; tests assume Redis is running there.
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    from app import config

    original = config.REDIS_URL
    monkeypatch.setattr(config, "REDIS_URL", redis_url)
    try:
        with TestClient(app) as client:
            yield client
    finally:
        config.REDIS_URL = original


def test_presence_heartbeat_and_sessions(client_with_redis):
    client = client_with_redis
    body = {
        "user_id": "u1",
        "session_id": "s1",
        "metadata": {"ip": "127.0.0.1"},
    }
    r = client.post("/api/v1/presence/heartbeat", json=body)
    assert r.status_code == 200
    sessions = client.get("/api/v1/presence/sessions", params={"user_id": "u1"})
    assert sessions.status_code == 200
    data = sessions.json()
    assert any(s["session_id"] == "s1" for s in data)


def test_locks_acquire_and_release(client_with_redis):
    client = client_with_redis
    body = {
        "user_id": "u1",
        "session_id": "s1",
        "project": "P1",
        "asset": "A1",
        "representation": "model",
    }
    r1 = client.post("/api/v1/locks/acquire", json=body)
    assert r1.status_code == 200
    status = client.get(
        "/api/v1/locks/status",
        params={"project": "P1", "asset": "A1", "representation": "model"},
    )
    assert status.status_code == 200
    assert status.json()["owner_session_id"] == "s1"

    # Idempotent acquire for same session
    r2 = client.post("/api/v1/locks/acquire", json=body)
    assert r2.status_code == 200

    # Release
    rel = client.post("/api/v1/locks/release", json=body)
    assert rel.status_code == 200
    status2 = client.get(
        "/api/v1/locks/status",
        params={"project": "P1", "asset": "A1", "representation": "model"},
    )
    assert status2.status_code == 404


def test_locks_conflict_for_other_session(client_with_redis):
    client = client_with_redis
    body1 = {
        "user_id": "u1",
        "session_id": "s1",
        "project": "P1",
        "asset": "A1",
        "representation": "model",
    }
    body2 = {
        "user_id": "u2",
        "session_id": "s2",
        "project": "P1",
        "asset": "A1",
        "representation": "model",
    }
    client.post("/api/v1/locks/acquire", json=body1)
    r = client.post("/api/v1/locks/acquire", json=body2)
    assert r.status_code == 409


def test_locks_release_not_owned(client_with_redis):
    client = client_with_redis
    body1 = {
        "user_id": "u1",
        "session_id": "s1",
        "project": "P1",
        "asset": "A1",
        "representation": "model",
    }
    body2 = {
        "user_id": "u2",
        "session_id": "s2",
        "project": "P1",
        "asset": "A1",
        "representation": "model",
    }
    client.post("/api/v1/locks/acquire", json=body1)
    r = client.post("/api/v1/locks/release", json=body2)
    assert r.status_code == 403

