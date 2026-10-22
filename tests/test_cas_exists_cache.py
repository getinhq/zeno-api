"""The CAS existence cache must never hide a blob that was uploaded after a 'missing' answer."""
import os

import httpx
import pytest
from blake3 import blake3

import app.config as config

pytestmark = pytest.mark.skipif(not os.environ.get("REDIS_URL"), reason="needs Redis")


@pytest.fixture
async def cas_client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CAS_ROOT", str(tmp_path))
    monkeypatch.setattr(config, "CAS_STORAGE_BACKEND", "nas")
    monkeypatch.setattr(config, "MONGO_URI", None)
    monkeypatch.setattr(config, "ENABLE_AUTH", False)
    from main import app

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def test_exists_check_then_upload_then_exists(cas_client):
    from app.cas.cache import get_cached_exists

    body = os.urandom(64)
    h = blake3(body).hexdigest()
    assert (await cas_client.get(f"/api/v1/cas/blobs/{h}/exists")).status_code == 404
    assert await get_cached_exists(h) is None  # "missing" is not cached
    assert (await cas_client.post("/api/v1/cas/blobs", content=body, headers={"X-Content-Hash": h})).status_code == 201
    assert await get_cached_exists(h) is True  # the upload is remembered
    assert (await cas_client.get(f"/api/v1/cas/blobs/{h}/exists")).status_code == 200

    other = os.urandom(64)
    h2 = blake3(other).hexdigest()
    await cas_client.get(f"/api/v1/cas/blobs/{h2}/exists")
    assert (await cas_client.put(f"/api/v1/cas/blobs/{h2}", content=other)).status_code == 201
    assert (await cas_client.get(f"/api/v1/cas/blobs/{h2}/exists")).status_code == 200
