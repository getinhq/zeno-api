"""Core REST API tests for projects and assets."""
import json
from uuid import uuid4

from fastapi.testclient import TestClient

from main import app


def test_projects_crud_flow():
    client = TestClient(app)

    # Create project
    body = {"name": "My Show", "code": "MS01"}
    r = client.post("/api/v1/projects", json=body)
    assert r.status_code == 200 or r.status_code == 201
    data = r.json()
    project_id = data["id"]

    # Get project
    r2 = client.get(f"/api/v1/projects/{project_id}")
    assert r2.status_code == 200

    # Patch project
    r3 = client.patch(f"/api/v1/projects/{project_id}", json={"status": "on_hold"})
    assert r3.status_code == 200
    assert r3.json()["status"] == "on_hold"

    # Soft-delete project
    r4 = client.delete(f"/api/v1/projects/{project_id}")
    assert r4.status_code == 200
    assert r4.json()["status"] == "archived"


def test_assets_crud_flow():
    client = TestClient(app)

    # Create a project first
    pr = client.post("/api/v1/projects", json={"name": "AssetProj", "code": "AP01"})
    assert pr.status_code in (200, 201)
    project_id = pr.json()["id"]

    # Create asset
    body = {"type": "character", "name": "Hero", "code": "hero"}
    ar = client.post(f"/api/v1/projects/{project_id}/assets", json=body)
    assert ar.status_code in (200, 201)
    asset = ar.json()
    asset_id = asset["id"]

    # List assets for project
    lr = client.get(f"/api/v1/projects/{project_id}/assets")
    assert lr.status_code == 200
    assert any(a["id"] == asset_id for a in lr.json())

    # Patch asset
    ur = client.patch(f"/api/v1/assets/{asset_id}", json={"name": "Hero_v2"})
    assert ur.status_code == 200
    assert ur.json()["name"] == "Hero_v2"

