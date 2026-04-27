"""Tests for /projects and /settings API. Require DATABASE_URL and MONGO_URI for full run."""
import os
import uuid
import pytest
from fastapi.testclient import TestClient

from main import app

client = TestClient(app)

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
MONGO_URI = os.environ.get("MONGO_URI", "").strip()
has_db = bool(DATABASE_URL)
has_mongo = bool(MONGO_URI)


@pytest.mark.skipif(not has_mongo, reason="MONGO_URI not set")
def test_settings_global():
    r = client.get("/api/v1/settings/global?env=development")
    assert r.status_code == 200
    data = r.json()
    assert data["env"] == "development"
    assert "resolution" in data
    assert "frame" in data


@pytest.mark.skipif(not has_mongo, reason="MONGO_URI not set")
def test_settings_effective():
    r = client.get("/api/v1/settings/effective?env=development")
    assert r.status_code == 200
    data = r.json()
    assert "resolution" in data


@pytest.mark.skipif(not has_db, reason="DATABASE_URL not set")
def test_projects_list():
    r = client.get("/api/v1/projects")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


@pytest.mark.skip(reason="TestClient + asyncpg pool: connection release fails in test context. Use curl or live server to test POST /projects.")
@pytest.mark.skipif(not has_db, reason="DATABASE_URL not set")
def test_projects_create():
    """POST /projects: manually test with curl -X POST .../projects -H 'Content-Type: application/json' -d '{\"name\":\"X\",\"code\":\"Y\"}'"""
    code = "E2E_" + str(uuid.uuid4()).replace("-", "")[:8]
    name = "E2E Test " + code
    r = client.post("/api/v1/projects", json={"name": name, "code": code})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["name"] == name
    assert data["code"] == code
    assert "id" in data


@pytest.mark.skipif(not has_mongo, reason="MONGO_URI not set")
def test_project_stage_mapping_roundtrip():
    project_id = str(uuid.uuid4())
    body = {
        "extra": {
            "stage_dcc_mapping": {
                "Animatics": "maya",
                "Layout": "maya",
                "Animation": "maya",
                "Lighting": "maya",
                "Comp": "nuke",
            }
        }
    }
    put_resp = client.put(f"/api/v1/settings/project/{project_id}", json=body)
    assert put_resp.status_code == 200, put_resp.text
    get_resp = client.get(f"/api/v1/settings/project/{project_id}")
    assert get_resp.status_code == 200, get_resp.text
    mapping = get_resp.json().get("extra", {}).get("stage_dcc_mapping", {})
    assert mapping.get("Comp") == "nuke"
