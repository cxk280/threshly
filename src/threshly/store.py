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

        self.bucket = bucket
        self.prefix = prefix
        endpoint = os.environ.get("THRESHLY_S3_ENDPOINT")  # set for MinIO
        self.client = boto3.client("s3", endpoint_url=endpoint)

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
