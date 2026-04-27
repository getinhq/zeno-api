"""Unit tests for ``app.auth.service`` — no DB required."""
from __future__ import annotations

import time

import jwt
import pytest

from app.auth import service as auth_service


def test_password_hash_round_trips():
    raw = "s3cret-passw0rd"
    h = auth_service.hash_password(raw)
    assert h and h != raw
    assert auth_service.verify_password(raw, h) is True
    assert auth_service.verify_password("wrong", h) is False
    assert auth_service.verify_password("anything", "") is False


def test_access_and_refresh_tokens_decode():
    access, _aexp = auth_service.issue_access_token(
        user_id="00000000-0000-0000-0000-000000000001",
        username="alice",
        app_role="pipeline",
    )
    refresh, _rexp, rjti = auth_service.issue_refresh_token(
        user_id="00000000-0000-0000-0000-000000000001",
    )
    a = auth_service.decode_token(access)
    r = auth_service.decode_token(refresh)
    assert a["typ"] == "access"
    assert a["username"] == "alice"
    assert a["app_role"] == "pipeline"
    assert r["typ"] == "refresh"
    assert r["jti"] == rjti
    assert a["jti"] != r["jti"]


def test_expired_access_token_raises():
    token, _ = auth_service.issue_access_token(
        user_id="00000000-0000-0000-0000-000000000001",
        username="alice",
        app_role="artist",
        ttl_seconds=1,
    )
    time.sleep(1.2)
    with pytest.raises(jwt.ExpiredSignatureError):
        auth_service.decode_token(token)


def test_tampered_token_rejected():
    token, _ = auth_service.issue_access_token(
        user_id="00000000-0000-0000-0000-000000000001",
        username="alice",
        app_role="artist",
    )
    tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    with pytest.raises(jwt.PyJWTError):
        auth_service.decode_token(tampered)
