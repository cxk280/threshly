"""Runtime configuration, read from environment with sane local defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class CoordinatorConfig:
    database_url: str = os.environ.get("THRESHLY_DATABASE_URL", "sqlite:///threshly.db")
    blob_dir: str = os.environ.get("THRESHLY_BLOB_DIR", "./threshly_data")
    blob_s3_bucket: str | None = os.environ.get("THRESHLY_S3_BUCKET") or None
    host: str = os.environ.get("THRESHLY_HOST", "0.0.0.0")
    port: int = int(os.environ.get("THRESHLY_PORT", "8080"))
    # How long a lease is valid before the reaper may reclaim it (seconds).
    lease_seconds: int = int(os.environ.get("THRESHLY_LEASE_SECONDS", "60"))
    # How often the reaper runs (seconds).
    reaper_interval: float = float(os.environ.get("THRESHLY_REAPER_INTERVAL", "5"))
    # Max items a worker may hold in a single lease.
    max_chunk: int = int(os.environ.get("THRESHLY_MAX_CHUNK", "32"))
    # A worker not heard from within this window is considered gone (seconds).
    worker_ttl: int = int(os.environ.get("THRESHLY_WORKER_TTL", "30"))
    # Max delivery attempts before an item is marked failed.
    max_attempts: int = int(os.environ.get("THRESHLY_MAX_ATTEMPTS", "5"))
    # Approximate spot price in USD per GPU-hour, for the cost estimate.
    spot_gpu_hourly_usd: float = float(os.environ.get("THRESHLY_SPOT_GPU_HOURLY_USD", "0.50"))


@dataclass(frozen=True)
class WorkerConfig:
    coordinator_url: str
    engine: str = "mock"
    model: str = "demo-model"
    lease_size: int = int(os.environ.get("THRESHLY_LEASE_SIZE", "16"))
    lease_seconds: int = int(os.environ.get("THRESHLY_LEASE_SECONDS", "60"))
    heartbeat_interval: float = float(os.environ.get("THRESHLY_HEARTBEAT_INTERVAL", "10"))
    poll_idle_seconds: float = float(os.environ.get("THRESHLY_POLL_IDLE_SECONDS", "1.0"))
    metrics_port: int = int(os.environ.get("THRESHLY_WORKER_METRICS_PORT", "9100"))
