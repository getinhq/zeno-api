"""Tests for Register-Version API: POST /api/v1/versions."""
import hashlib
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.fixture
def client_with_db_and_cas(tmp_path, monkeypatch):
    """TestClient with DATABASE_URL and CAS_ROOT configured."""
    # Configure CAS root
    cas_root = tmp_path / "cas"
    cas_root.mkdir()

    # Minimal Postgres config: reuse existing env-based DATABASE_URL if present.
    # Tests that hit the DB are expected to run in an environment where DATABASE_URL points
    # to a test database with the schema applied.
    from app import config
    from app.cas import router as cas_router

    original_cas_root_config = config.CAS_ROOT
    original_cas_root_router = cas_router.CAS_ROOT

    config.CAS_ROOT = str(cas_root)
    cas_router.CAS_ROOT = str(cas_root)

    try:
        client = TestClient(app)
    finally:
        # Restore CAS_ROOTs for any subsequent tests
        config.CAS_ROOT = original_cas_root_config
        cas_router.CAS_ROOT = original_cas_root_router

    return client


def test_register_version_invalid_content_id(client_with_db_and_cas):
    client = client_with_db_and_cas
    body = {
        "project": "P1",
        "asset": "A1",
        "representation": "model",
        "version": "next",
        "content_id": "short",
    }
    resp = client.post("/api/v1/versions", json=body)
    # Pydantic body validation error
    assert resp.status_code == 422


def test_register_version_invalid_version_spec(client_with_db_and_cas):
    client = client_with_db_and_cas
    body = {
        "project": "P1",
        "asset": "A1",
        "representation": "model",
        "version": "banana",
        "content_id": "a" * 64,
    }
    resp = client.post("/api/v1/versions", json=body)
    # Pydantic body validation error
    assert resp.status_code == 422


def test_register_version_cas_missing(monkeypatch, client_with_db_and_cas):
    client = client_with_db_and_cas

    # Monkeypatch CAS exists to always return False
    from app.versions import service as versions_service

    def fake_ensure_exists(content_id: str) -> None:
        raise versions_service.ContentNotFoundInCas("CAS content not found")

    monkeypatch.setattr(versions_service, "_ensure_cas_content_exists", fake_ensure_exists)

    body = {
        "project": str(uuid4()),
        "asset": str(uuid4()),
        "representation": "model",
        "version": "1",
        "content_id": "a" * 64,
    }
    resp = client.post("/api/v1/versions", json=body)
    # Project/asset are fake UUIDs and will not resolve, so this returns 404.
    assert resp.status_code == 404


def test_register_version_db_unavailable(monkeypatch, client_with_db_and_cas):
    client = client_with_db_and_cas

    from app.versions import service as versions_service

    async def failing_register_version(data):
        raise versions_service.ServiceUnavailable("DB down")

    monkeypatch.setattr(versions_service, "register_version", failing_register_version)

    body = {
        "project": "P1",
        "asset": "A1",
        "representation": "model",
        "version": "next",
        "content_id": "a" * 64,
    }
    resp = client.post("/api/v1/versions", json=body)
    assert resp.status_code == 503

