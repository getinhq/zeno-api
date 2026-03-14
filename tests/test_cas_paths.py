"""Unit tests: hash_to_path and is_valid_hash."""
import pytest
from pathlib import Path

from app.cas.paths import hash_to_path, is_valid_hash


def test_is_valid_hash_accepts_64_lowercase_hex():
    assert is_valid_hash("a" * 64) is True
    assert is_valid_hash("0" * 64) is True
    assert is_valid_hash("0123456789abcdef" * 4) is True


def test_is_valid_hash_rejects_non_hex():
    assert is_valid_hash("g" + "a" * 63) is False
    assert is_valid_hash("A" + "a" * 63) is False  # uppercase


def test_is_valid_hash_rejects_wrong_length():
    assert is_valid_hash("a" * 63) is False
    assert is_valid_hash("a" * 65) is False
    assert is_valid_hash("") is False


def test_hash_to_path_layout():
    root = Path("/cas")
    path = hash_to_path(root, "a1b2c3d4" + "e" * 56)
    assert path == Path("/cas/a1/b2/a1b2c3d4" + "e" * 56)
    assert path.parent.parent.parent == root


def test_hash_to_path_raises_on_invalid():
    with pytest.raises(ValueError, match="Invalid hash"):
        hash_to_path(Path("/cas"), "bad")
    with pytest.raises(ValueError, match="Invalid hash"):
        hash_to_path(Path("/cas"), "A1" + "a" * 62)
