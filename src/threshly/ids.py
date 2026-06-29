"""Stable id generation for batches, files, items, and workers."""

from __future__ import annotations

import secrets


def _rand(n: int = 24) -> str:
    return secrets.token_hex(n // 2)


def batch_id() -> str:
    return f"batch_{_rand()}"


def file_id() -> str:
    return f"file_{_rand()}"


def item_id() -> str:
    return f"item_{_rand()}"


def worker_id(suffix: str | None = None) -> str:
    base = f"wkr_{_rand(12)}"
    return f"{base}-{suffix}" if suffix else base
