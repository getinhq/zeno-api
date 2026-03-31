"""Launch token mint/exchange and open-lock-check."""
from __future__ import annotations

import os
import uuid
from typing import Optional

import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.fixture
def client_with_redis(monkeypatch):
    """
    Point Redis at localhost (Docker-only hostnames in .env break pytest on the host).
    Disable Postgres/Mongo for this module so lifespan does not open pools to unreachable hosts.
    """
    redis_url = os.environ.get("PYTEST_REDIS_URL", "redis://127.0.0.1:6379/0")
    from app import config

    monkeypatch.setattr(config, "REDIS_URL", redis_url)
    monkeypatch.setattr(config, "DATABASE_URL", None)
    monkeypatch.setattr(config, "MONGO_URI", None)
    monkeypatch.setattr(config, "APP_ENV", "development")
    monkeypatch.setattr(config, "ZENO_LAUNCH_MINT_SECRET", None)
    with TestClient(app) as client:
        yield client


def _sample_context(project_id: str, asset_id: Optional[str] = None):
    ctx = {
        "version": "1",
        "intent": "open_asset",
        "project_id": project_id,
        "dcc": "blender",
    }
    if asset_id:
        ctx["asset_id"] = asset_id
    return ctx


def test_mint_and_exchange_once(client_with_redis):
    pid = str(uuid.uuid4())
    aid = str(uuid.uuid4())
    r = client_with_redis.post(
        "/api/v1/launch-tokens",
        json={"context": _sample_context(pid, aid)},
    )
    assert r.status_code == 200
    token = r.json()["token"]

    ex = client_with_redis.get(f"/api/v1/launch-tokens/{token}")
    assert ex.status_code == 200
    assert ex.json()["context"]["project_id"] == pid
    assert ex.json()["context"]["asset_id"] == aid

    again = client_with_redis.get(f"/api/v1/launch-tokens/{token}")
    assert again.status_code == 410


def test_mint_requires_secret_in_production(client_with_redis, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "APP_ENV", "production")
    monkeypatch.setattr(config, "ZENO_LAUNCH_MINT_SECRET", None)
    r = client_with_redis.post(
        "/api/v1/launch-tokens",
        json={"context": _sample_context(str(uuid.uuid4()))},
    )
    assert r.status_code == 503


def test_mint_with_secret_header(client_with_redis, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "APP_ENV", "production")
    monkeypatch.setattr(config, "ZENO_LAUNCH_MINT_SECRET", "test-secret")
    r = client_with_redis.post(
        "/api/v1/launch-tokens",
        json={"context": _sample_context(str(uuid.uuid4()))},
    )
    assert r.status_code == 401

    r2 = client_with_redis.post(
        "/api/v1/launch-tokens",
        json={"context": _sample_context(str(uuid.uuid4()))},
        headers={"X-Zeno-Launch-Mint-Key": "test-secret"},
    )
    assert r2.status_code == 200


def test_open_lock_check_not_blocked(client_with_redis):
    p = str(uuid.uuid4())
    a = str(uuid.uuid4())
    r = client_with_redis.get(
        "/api/v1/launch/open-lock-check",
        params={"project": p, "asset": a, "representation": "model"},
    )
    assert r.status_code == 200
    assert r.json()["blocked"] is False


def test_open_lock_check_blocked(client_with_redis):
    # Use unique ids so we never collide with stale/corrupt lock:* keys in a dev Redis.
    p = str(uuid.uuid4())
    a = str(uuid.uuid4())
    rep = "model"
    acq = client_with_redis.post(
        "/api/v1/locks/acquire",
        json={
            "user_id": "u1",
            "session_id": "s1",
            "project": p,
            "asset": a,
            "representation": rep,
        },
    )
    assert acq.status_code == 200, acq.text
    r = client_with_redis.get(
        "/api/v1/launch/open-lock-check",
        params={"project": p, "asset": a, "representation": rep},
    )
    assert r.status_code == 200
    assert r.json()["blocked"] is True
    assert r.json()["lock"]["owner_user_id"] == "u1"
