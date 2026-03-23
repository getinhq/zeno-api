"""S3-compatible CAS backend (MinIO, AWS): hash-keyed objects in a single bucket."""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import BinaryIO, Iterator, Union

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.cas.backend import HashMismatchError
from app.cas.paths import is_valid_hash


class S3Backend:
    """CAS backend storing blobs in S3/MinIO; object key is the lowercase SHA-256 hex string."""

    def __init__(
        self,
        endpoint_url: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        *,
        region_name: str = "us-east-1",
    ) -> None:
        self.bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
            region_name=region_name,
        )

    def _key(self, hash_str: str) -> str:
        return hash_str.lower()

    def exists(self, hash_str: str) -> bool:
        if not is_valid_hash(hash_str):
            return False
        try:
            self._client.head_object(Bucket=self.bucket, Key=self._key(hash_str))
            return True
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    def get_size(self, hash_str: str) -> int:
        if not is_valid_hash(hash_str):
            raise ValueError("Invalid hash: must be 64 lowercase hex characters")
        try:
            r = self._client.head_object(Bucket=self.bucket, Key=self._key(hash_str))
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                raise FileNotFoundError(f"Blob not found: {hash_str[:16]}...") from e
            raise
        return int(r["ContentLength"])

    def get_stream(self, hash_str: str) -> Iterator[bytes]:
        if not is_valid_hash(hash_str):
            raise ValueError("Invalid hash: must be 64 lowercase hex characters")
        try:
            obj = self._client.get_object(Bucket=self.bucket, Key=self._key(hash_str))
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                raise FileNotFoundError(f"Blob not found: {hash_str[:16]}...") from e
            raise
        body = obj["Body"]
        try:
            while chunk := body.read(65536):
                yield chunk
        finally:
            body.close()

    def put_stream(self, hash_str: str, stream: BinaryIO) -> bool:
        if not is_valid_hash(hash_str):
            raise ValueError("Invalid hash: must be 64 lowercase hex characters")
        key = self._key(hash_str)
        if self.exists(hash_str):
            return False

        tmp_dir = tempfile.mkdtemp(prefix="cas_s3_")
        tmp_path = os.path.join(tmp_dir, "blob")
        hasher = hashlib.sha256()
        try:
            with open(tmp_path, "wb") as f:
                while chunk := stream.read(65536):
                    hasher.update(chunk)
                    f.write(chunk)
            computed = hasher.hexdigest()
            if computed != hash_str:
                raise HashMismatchError(
                    f"Content hash mismatch: expected {hash_str[:16]}..., got {computed[:16]}..."
                )
            self._client.upload_file(tmp_path, self.bucket, key)
            return True
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            try:
                os.rmdir(tmp_dir)
            except OSError:
                pass

    def put_from_path(self, hash_str: str, source_path: Path) -> bool:
        """Upload file at source_path; caller must ensure content hashes to hash_str."""
        if not is_valid_hash(hash_str):
            raise ValueError("Invalid hash: must be 64 lowercase hex characters")
        key = self._key(hash_str)
        if self.exists(hash_str):
            return False
        self._client.upload_file(str(Path(source_path).resolve()), self.bucket, key)
        return True

    def ensure_tmp(self) -> Path:
        """Directory for streaming uploads before verification (same pattern as NAS)."""
        p = Path(tempfile.mkdtemp(prefix="cas_s3_chunk_"))
        return p
