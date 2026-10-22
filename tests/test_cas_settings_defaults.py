"""Unsaved settings must not override the server's CAS configuration."""
import app.config as cfg
from app.settings import store


def test_defaults_follow_environment(monkeypatch):
    monkeypatch.setattr(cfg, "CAS_STORAGE_BACKEND", "nas")
    monkeypatch.setattr(cfg, "CAS_ROOT", "/data/cas")
    monkeypatch.setattr(cfg, "S3_ENDPOINT_URL", None)
    assert store._env_cas_defaults() == {"use_minio": False, "local_cas_root": "/data/cas"}

    monkeypatch.setattr(cfg, "CAS_STORAGE_BACKEND", "auto")
    monkeypatch.setattr(cfg, "S3_ENDPOINT_URL", "http://minio:9000")
    monkeypatch.setattr(cfg, "S3_ACCESS_KEY", "k")
    monkeypatch.setattr(cfg, "S3_SECRET_KEY", "s")
    assert store._env_cas_defaults()["use_minio"] is True
