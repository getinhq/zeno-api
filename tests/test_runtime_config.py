"""Production startup guards."""
import pytest

import app.config as cfg
from main import _validate_runtime_config


@pytest.fixture
def prod(monkeypatch):
    monkeypatch.setattr(cfg, "APP_ENV", "production")
    monkeypatch.setattr(cfg, "ENABLE_AUTH", True)
    monkeypatch.setattr(cfg, "ZENO_JWT_SECRET", "x" * 40)
    monkeypatch.setattr(cfg, "CAS_STORAGE_BACKEND", "auto")
    monkeypatch.setattr(cfg, "CAS_ROOT", "/data/cas")
    monkeypatch.setattr(cfg, "S3_ENDPOINT_URL", None)
    monkeypatch.setattr(cfg, "PUBLIC_APP_URL", "https://zeno.example.com")
    return monkeypatch


def test_local_disk_storage_is_allowed_in_production(prod):
    _validate_runtime_config()


@pytest.mark.parametrize(
    "attr,value,message",
    [
        ("ENABLE_AUTH", False, "ENABLE_AUTH"),
        ("ZENO_JWT_SECRET", "short", "ZENO_JWT_SECRET"),
        ("CAS_ROOT", None, "Configure CAS storage"),
        ("PUBLIC_APP_URL", "http://localhost:5173", "ZENO_PUBLIC_APP_URL"),
    ],
)
def test_unsafe_production_config_is_rejected(prod, attr, value, message):
    prod.setattr(cfg, attr, value)
    with pytest.raises(RuntimeError, match=message):
        _validate_runtime_config()


def test_development_is_not_checked(monkeypatch):
    monkeypatch.setattr(cfg, "APP_ENV", "development")
    monkeypatch.setattr(cfg, "ENABLE_AUTH", False)
    _validate_runtime_config()
