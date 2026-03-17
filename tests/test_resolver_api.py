"""Tests for Resolver API GET/POST /api/v1/resolve."""
import os
import pytest
from fastapi.testclient import TestClient

from main import app

client = TestClient(app)

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
has_db = bool(DATABASE_URL)


def test_resolve_400_invalid_scheme():
    r = client.get("/api/v1/resolve", params={"uri": "http://foo/bar/latest/fbx"})
    assert r.status_code == 400
    assert "scheme" in r.json().get("detail", "").lower()


def test_resolve_400_wrong_segment_count():
    r = client.get("/api/v1/resolve", params={"uri": "asset://proj/asset/latest"})
    assert r.status_code == 400
    assert "segment" in r.json().get("detail", "").lower()


def test_resolve_400_invalid_version():
    r = client.get("/api/v1/resolve", params={"uri": "asset://proj/asset/notanumber/fbx"})
    assert r.status_code == 400
    assert "version" in r.json().get("detail", "").lower()


def test_resolve_400_negative_version():
    r = client.get("/api/v1/resolve", params={"uri": "asset://proj/asset/-1/fbx"})
    assert r.status_code == 400


def test_resolve_post_400_missing_uri():
    r = client.post("/api/v1/resolve", json={})
    assert r.status_code == 400
    assert "uri" in r.json().get("detail", "").lower()


def test_resolve_post_400_invalid_scheme():
    r = client.post("/api/v1/resolve", json={"uri": "file:///path/to/thing"})
    assert r.status_code == 400


@pytest.mark.skipif(not has_db, reason="DATABASE_URL not set")
def test_resolve_404_unknown_project():
    r = client.get(
        "/api/v1/resolve",
        params={"uri": "asset://nonexistent_project_xyz/asset/latest/fbx"},
    )
    assert r.status_code in (404, 503)
    assert "detail" in r.json()


@pytest.mark.skipif(not has_db, reason="DATABASE_URL not set")
def test_resolve_200_shape_when_data_exists():
    """If DB has project MS01, asset hero, version 1 rep fbx, resolve returns content_id, filename, size."""
    r = client.get(
        "/api/v1/resolve",
        params={"uri": "asset://MS01/hero_model/latest/fbx"},
    )
    if r.status_code == 404:
        pytest.skip("No test data (project MS01, asset hero_model, version with rep fbx)")
    if r.status_code == 503:
        pytest.skip("DB connection unavailable in test context")
    assert r.status_code == 200
    data = r.json()
    assert "content_id" in data
    assert "filename" in data
    assert "size" in data
    assert isinstance(data["content_id"], str)
    assert isinstance(data["filename"], str)
    assert data["size"] is None or isinstance(data["size"], int)


def test_resolve_uri_parser_latest_case_insensitive():
    """POST with 'LATEST' in URI is valid (case-insensitive)."""
    r = client.post(
        "/api/v1/resolve",
        json={"uri": "asset://proj/asset/LATEST/fbx"},
    )
    assert r.status_code in (200, 404, 503)
