"""NAS-backed CAS: exists, get_stream, put_stream with hash verification and atomic move."""
import os
import tempfile
from pathlib import Path
from typing import BinaryIO, Iterator, Union

from blake3 import blake3

from app.cas.paths import hash_to_path, is_valid_hash


class HashMismatchError(Exception):
    """Raised when stream content digest does not match the expected hash."""


class NASBackend:
    """CAS backend storing blobs on local/NAS filesystem by BLAKE3 hash."""

    def __init__(self, root: Union[str, Path]) -> None:
        self.root = Path(root).resolve()
        self._tmp_dir = self.root / ".tmp"

    def _ensure_tmp(self) -> Path:
        self._tmp_dir.mkdir(parents=True, exist_ok=True)
        return self._tmp_dir

    def ensure_tmp(self) -> Path:
        """Directory for verified streaming uploads (compat with S3 backend)."""
        return self._ensure_tmp()

    def exists(self, hash_str: str) -> bool:
        """Return True if a blob with this hash exists."""
        if not is_valid_hash(hash_str):
            return False
        path = hash_to_path(self.root, hash_str)
        return path.is_file()

    def get_size(self, hash_str: str) -> int:
        """Return blob size in bytes. Raises FileNotFoundError if not found."""
        if not is_valid_hash(hash_str):
            raise ValueError("Invalid hash: must be 64 lowercase hex characters")
        path = hash_to_path(self.root, hash_str)
        if not path.is_file():
            raise FileNotFoundError(f"Blob not found: {hash_str[:16]}...")
        return path.stat().st_size

    def get_path(self, hash_str: str) -> Path:
        """Return the Path for a blob; does not check existence."""
        return hash_to_path(self.root, hash_str)

    def get_stream(self, hash_str: str) -> Iterator[bytes]:
        """Stream blob bytes. Raises FileNotFoundError if not found."""
        if not is_valid_hash(hash_str):
            raise ValueError(f"Invalid hash: must be 64 lowercase hex characters")
        path = hash_to_path(self.root, hash_str)
        if not path.is_file():
            raise FileNotFoundError(f"Blob not found: {hash_str[:16]}...")
        with open(path, "rb") as f:
            while chunk := f.read(65536):
                yield chunk

    def put_stream(self, hash_str: str, stream: BinaryIO) -> bool:
        """
        Write stream to CAS: temp file + BLAKE3 on the fly, then atomic move.
        Returns True if created (201), False if already existed (200 idempotent).
        Raises HashMismatchError if content hash != hash_str.
        """
        if not is_valid_hash(hash_str):
            raise ValueError(f"Invalid hash: must be 64 lowercase hex characters")
        target = hash_to_path(self.root, hash_str)
        if target.is_file():
            return False  # idempotent: already exists

        tmp_dir = self._ensure_tmp()
        hasher = blake3()
        fd, tmp_path = tempfile.mkstemp(dir=tmp_dir, prefix="blob_")
        try:
            with os.fdopen(fd, "wb") as f:
                while chunk := stream.read(65536):
                    hasher.update(chunk)
                    f.write(chunk)
            computed = hasher.hexdigest()
            if computed != hash_str:
                os.unlink(tmp_path)
                raise HashMismatchError(
                    f"Content hash mismatch: expected {hash_str[:16]}..., got {computed[:16]}..."
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(tmp_path, target)
            return True
        except Exception:
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            raise

    def put_from_path(self, hash_str: str, source_path: Path) -> bool:
        """
        Move an existing file (e.g. temp) to the CAS location. Caller must ensure
        the file content hashes to hash_str. Returns True if created, False if already existed.
        """
        if not is_valid_hash(hash_str):
            raise ValueError("Invalid hash: must be 64 lowercase hex characters")
        target = hash_to_path(self.root, hash_str)
        if target.is_file():
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(Path(source_path).resolve(), target)
        return True
