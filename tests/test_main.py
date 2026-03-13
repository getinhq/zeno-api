import pytest
from fastapi.testclient import TestClient

from main import app

client = TestClient(app)


def test_root():
    r = client.get("/")
    assert r.status_code == 200
    data = r.json()
    assert data["service"] == "zeno-api"
    assert "version" in data


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert "postgres" in data
    assert "redis" in data
    assert "mongo" in data
    assert "minio" in data
