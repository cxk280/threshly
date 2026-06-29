"""Pydantic schemas for the HTTP API.

Public objects mirror the OpenAI Batch API shape so existing clients work unchanged. The
``/internal/*`` payloads are Threshly's worker<->coordinator protocol.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


# ---- OpenAI-compatible public objects ----------------------------------------------------------
class FileObjectOut(BaseModel):
    id: str
    object: str = "file"
    bytes: int
    created_at: int
    filename: str
    purpose: str = "batch"


class RequestCounts(BaseModel):
    total: int = 0
    completed: int = 0
    failed: int = 0


class BatchOut(BaseModel):
    id: str
    object: str = "batch"
    endpoint: str
    input_file_id: str
    completion_window: str = "24h"
    status: str
    output_file_id: str | None = None
    error_file_id: str | None = None
    model: str
    created_at: int
    in_progress_at: int | None = None
    finalizing_at: int | None = None
    completed_at: int | None = None
    cancelled_at: int | None = None
    request_counts: RequestCounts = Field(default_factory=RequestCounts)
    metadata: dict[str, Any] | None = None
    errors: dict[str, Any] | None = None


class CreateBatchIn(BaseModel):
    input_file_id: str
    endpoint: str = "/v1/chat/completions"
    completion_window: str = "24h"
    model: str | None = None  # may be inferred from request bodies if omitted
    metadata: dict[str, Any] | None = None


# ---- Internal worker protocol ------------------------------------------------------------------
class LeaseRequest(BaseModel):
    worker_id: str
    engine: str = "mock"
    model: str = ""
    max_items: int = 16
    lease_seconds: int = 60


class LeaseItem(BaseModel):
    item_id: str
    batch_id: str
    custom_id: str
    body: dict[str, Any]
    prefix_group: str = ""


class LeaseResponse(BaseModel):
    items: list[LeaseItem] = Field(default_factory=list)
    lease_expires_at: float | None = None


class CompletedResult(BaseModel):
    item_id: str
    response: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    prompt_tokens: int = 0
    output_tokens: int = 0
    cache_hit: bool = False


class CompleteRequest(BaseModel):
    worker_id: str
    results: list[CompletedResult] = Field(default_factory=list)


class CompleteResponse(BaseModel):
    accepted: int
    duplicates: int


class HeartbeatRequest(BaseModel):
    worker_id: str
    engine: str = "mock"
    model: str = ""
    holding: list[str] = Field(default_factory=list)
    lease_seconds: int = 60


class HeartbeatResponse(BaseModel):
    ok: bool = True
    extended: int = 0
    # Items the coordinator no longer considers this worker's (e.g. reclaimed); worker should drop.
    revoked: list[str] = Field(default_factory=list)
