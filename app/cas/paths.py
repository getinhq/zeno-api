"""CAS path resolution: hash -> filesystem path (first2/next2/fullhash)."""
import re
from pathlib import Path

# SHA-256 = 64 lowercase hex characters
HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def is_valid_hash(hash_str: str) -> bool:
    """Return True if hash_str is a valid 64-char lowercase hex SHA-256."""
    return bool(hash_str and HASH_PATTERN.match(hash_str))


def hash_to_path(root: Path, hash_str: str) -> Path:
    """Return the absolute path for a blob: root/{first2}/{next2}/{fullhash} (file)."""
    if not is_valid_hash(hash_str):
        raise ValueError(f"Invalid hash: must be 64 lowercase hex characters, got {len(hash_str) or 0} chars")
    root = Path(root).resolve()
    first2 = hash_str[:2]
    next2 = hash_str[2:4]
    return root / first2 / next2 / hash_str
