"""Tests for Register-Version API: POST /api/v1/versions."""
import hashlib
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.fixture
def client_with_db_and_cas(tmp_path, monkeypatch):
    """TestClient with DATABASE_URL and CAS_ROOT configured for the duration of the test."""
    cas_root = tmp_path / "cas"
    cas_root.mkdir()

    from app import config

    original_cas_root = config.CAS_ROOT
    original_backend = config.CAS_STORAGE_BACKEND
    original_s3e = config.S3_ENDPOINT_URL
    original_s3a = config.S3_ACCESS_KEY
    original_s3s = config.S3_SECRET_KEY

    config.CAS_ROOT = str(cas_root)
    config.CAS_STORAGE_BACKEND = "nas"
    config.S3_ENDPOINT_URL = None
    config.S3_ACCESS_KEY = None
    config.S3_SECRET_KEY = None

    try:
        yield TestClient(app)
    finally:
        config.CAS_ROOT = original_cas_root
        config.CAS_STORAGE_BACKEND = original_backend
        config.S3_ENDPOINT_URL = original_s3e
        config.S3_ACCESS_KEY = original_s3a
        config.S3_SECRET_KEY = original_s3s


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

    # Endpoint imports register_version; patch on router module so CAS miss is exercised.
    from app.versions import service as versions_service
    import app.versions.router as versions_router

    async def fake_register_version(data):
        raise versions_service.ContentNotFoundInCas("CAS content not found")

    monkeypatch.setattr(versions_router, "register_version", fake_register_version)

    body = {
        "project": str(uuid4()),
        "asset": str(uuid4()),
        "representation": "model",
        "version": "1",
        "content_id": "a" * 64,
    }
    resp = client.post("/api/v1/versions", json=body)
    assert resp.status_code == 409


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

