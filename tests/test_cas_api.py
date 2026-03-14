"""Integration-style tests: CAS PUT/GET/HEAD with temp CAS_ROOT."""
import hashlib
import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from main import app


# SHA-256 of b"hello"
HELLO_HASH = hashlib.sha256(b"hello").hexdigest()


@pytest.fixture
def cas_root(tmp_path):
    """Temp directory as CAS_ROOT."""
    root = tmp_path / "cas"
    root.mkdir()
    return str(root)


@pytest.fixture
def client_with_cas(cas_root, monkeypatch):
    monkeypatch.setenv("ZENO_CAS_ROOT", cas_root)
    from app import config
    from app.cas import router as cas_router
    monkeypatch.setattr(config, "CAS_ROOT", cas_root)
    monkeypatch.setattr(cas_router, "CAS_ROOT", cas_root)
    return TestClient(app)


def test_put_then_get(client_with_cas, cas_root):
    response = client_with_cas.put(
        f"/api/v1/cas/blobs/{HELLO_HASH}",
        content=b"hello",
    )
    assert response.status_code == 201
    # File at expected path
    path = Path(cas_root) / HELLO_HASH[:2] / HELLO_HASH[2:4] / HELLO_HASH
    assert path.is_file()
    assert path.read_bytes() == b"hello"

    get_resp = client_with_cas.get(f"/api/v1/cas/blobs/{HELLO_HASH}")
    assert get_resp.status_code == 200
    assert get_resp.content == b"hello"


def test_head_exists(client_with_cas):
    client_with_cas.put(f"/api/v1/cas/blobs/{HELLO_HASH}", content=b"hello")
    head = client_with_cas.head(f"/api/v1/cas/blobs/{HELLO_HASH}")
    assert head.status_code == 200
    assert head.headers.get("Content-Length") == "5"


def test_put_idempotent(client_with_cas):
    r1 = client_with_cas.put(f"/api/v1/cas/blobs/{HELLO_HASH}", content=b"hello")
    assert r1.status_code == 201
    r2 = client_with_cas.put(f"/api/v1/cas/blobs/{HELLO_HASH}", content=b"hello")
    assert r2.status_code == 200


def test_put_hash_mismatch_400(client_with_cas):
    # Claim hash is HELLO_HASH but send different body
    other_hash = hashlib.sha256(b"world").hexdigest()
    response = client_with_cas.put(
        f"/api/v1/cas/blobs/{HELLO_HASH}",
        content=b"world",
    )
    assert response.status_code == 400
    assert "mismatch" in response.text.lower()


def test_get_not_found_404(client_with_cas):
    missing = "a" * 64
    response = client_with_cas.get(f"/api/v1/cas/blobs/{missing}")
    assert response.status_code == 404


def test_head_not_found_404(client_with_cas):
    missing = "a" * 64
    response = client_with_cas.head(f"/api/v1/cas/blobs/{missing}")
    assert response.status_code == 404


def test_invalid_hash_400(client_with_cas):
    for bad in ["short", "G" + "a" * 63, "x" * 65]:
        r = client_with_cas.put(f"/api/v1/cas/blobs/{bad}", content=b"x")
        assert r.status_code == 400


def test_cas_not_configured_503():
    """Without ZENO_CAS_ROOT, CAS endpoints return 503."""
    with TestClient(app) as c:
        # Ensure CAS_ROOT is unset for this test
        from app import config
        original = config.CAS_ROOT
        config.CAS_ROOT = None
        try:
            r = c.put("/api/v1/cas/blobs/" + "a" * 64, content=b"x")
            assert r.status_code == 503
        finally:
            config.CAS_ROOT = original
