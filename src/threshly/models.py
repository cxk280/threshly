"""SQLAlchemy ORM models: the durable state behind the coordinator.

State machines
--------------
Batch:  validating -> in_progress -> finalizing -> completed
                                   \\-> cancelling -> cancelled
                                    \\-> failed
RequestItem:  pending -> leased -> done
                      \\-> leased -> pending (lease expired / preemption)
                       \\-> failed (exhausted attempts)
"""

from __future__ import annotations

import time

from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def now() -> float:
    return time.time()


class Base(DeclarativeBase):
    pass


class FileObject(Base):
    __tablename__ = "files"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    filename: Mapped[str] = mapped_column(String)
    purpose: Mapped[str] = mapped_column(String, default="batch")
    bytes: Mapped[int] = mapped_column(Integer, default=0)
    # Storage key in the blob store (path or object key).
    blob_key: Mapped[str] = mapped_column(String)
    created_at: Mapped[float] = mapped_column(Float, default=now)


class Batch(Base):
    __tablename__ = "batches"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    input_file_id: Mapped[str] = mapped_column(String, ForeignKey("files.id"))
    output_file_id: Mapped[str | None] = mapped_column(String, nullable=True)
    error_file_id: Mapped[str | None] = mapped_column(String, nullable=True)
    endpoint: Mapped[str] = mapped_column(String, default="/v1/chat/completions")
    model: Mapped[str] = mapped_column(String)
    completion_window: Mapped[str] = mapped_column(String, default="24h")
    status: Mapped[str] = mapped_column(String, default="validating", index=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    total: Mapped[int] = mapped_column(Integer, default=0)
    completed: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[float] = mapped_column(Float, default=now)
    in_progress_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    finalizing_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    completed_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    cancelled_at: Mapped[float | None] = mapped_column(Float, nullable=True)

    items: Mapped[list[RequestItem]] = relationship(back_populates="batch")


class RequestItem(Base):
    __tablename__ = "request_items"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    batch_id: Mapped[str] = mapped_column(String, ForeignKey("batches.id"), index=True)
    custom_id: Mapped[str] = mapped_column(String)
    seq: Mapped[int] = mapped_column(Integer)  # original line order
    # Hash bucket of the request's shared prefix, used to group cache-friendly work.
    prefix_group: Mapped[str] = mapped_column(String, index=True, default="")
    body_json: Mapped[str] = mapped_column(Text)

    status: Mapped[str] = mapped_column(String, default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    leased_by: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_expires_at: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)

    # Result, set on completion.
    response_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_hit: Mapped[int] = mapped_column(Integer, default=0)  # 0/1
    completed_at: Mapped[float | None] = mapped_column(Float, nullable=True)

    batch: Mapped[Batch] = relationship(back_populates="items")


# Composite index that backs the lease query: pending items, grouped for prefix locality.
Index("ix_items_dispatch", RequestItem.status, RequestItem.prefix_group, RequestItem.seq)


class Worker(Base):
    __tablename__ = "workers"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    engine: Mapped[str] = mapped_column(String, default="mock")
    model: Mapped[str] = mapped_column(String, default="")
    first_seen: Mapped[float] = mapped_column(Float, default=now)
    last_seen: Mapped[float] = mapped_column(Float, default=now, index=True)
    completed: Mapped[int] = mapped_column(Integer, default=0)


class Counter(Base):
    """Small key/value table for monotonic operational counters (e.g. preemptions)."""

    __tablename__ = "counters"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[float] = mapped_column(Float, default=0.0)
