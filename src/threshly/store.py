"""Blob store for input/output/error JSONL. Local filesystem by default; S3/MinIO optional."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol


class BlobStore(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...


class LocalBlobStore:
    def __init__(self, root: str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        p = self.root / key
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def put(self, key: str, data: bytes) -> None:
        self._path(key).write_bytes(data)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()


class S3BlobStore:
    """S3/MinIO-backed store. Requires the ``s3`` extra (boto3)."""

    def __init__(self, bucket: str, prefix: str = "threshly/") -> None:
        import boto3  # imported lazily so the core install stays light
        from botocore.config import Config

        self.bucket = bucket
        self.prefix = prefix
        endpoint = os.environ.get("THRESHLY_S3_ENDPOINT")  # set for MinIO
        # MinIO and most self-hosted S3 need path-style addressing (bucket in the path, not host).
        config = Config(s3={"addressing_style": "path"}) if endpoint else None
        self.client = boto3.client("s3", endpoint_url=endpoint, config=config)
        if endpoint:
            self._ensure_bucket(bucket)

    def _ensure_bucket(self, bucket: str, attempts: int = 30, delay: float = 1.0) -> None:
        """Create the bucket if needed, tolerating a MinIO/S3 endpoint still coming up."""
        import time

        last: Exception | None = None
        for _ in range(attempts):
            try:
                self.client.head_bucket(Bucket=bucket)
                return
            except Exception as e:  # not found, or endpoint not ready yet
                last = e
                try:
                    self.client.create_bucket(Bucket=bucket)
                    return
                except Exception as e2:
                    last = e2
                    time.sleep(delay)
        raise RuntimeError(f"could not reach/create S3 bucket {bucket!r}: {last}")

    def _key(self, key: str) -> str:
        return f"{self.prefix}{key}"

    def put(self, key: str, data: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=self._key(key), Body=data)

    def get(self, key: str) -> bytes:
        obj = self.client.get_object(Bucket=self.bucket, Key=self._key(key))
        return obj["Body"].read()

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.client.head_object(Bucket=self.bucket, Key=self._key(key))
            return True
        except ClientError:
            return False


def make_store(blob_dir: str, s3_bucket: str | None) -> BlobStore:
    if s3_bucket:
        return S3BlobStore(s3_bucket)
    return LocalBlobStore(blob_dir)
