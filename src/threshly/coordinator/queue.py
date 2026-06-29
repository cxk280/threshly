"""The durable lease queue — the core of Threshly's preemption tolerance.

Every request item is leased to a worker with a TTL. Workers heartbeat to extend leases and report
completions; completion is idempotent per item. A reaper reclaims expired leases so a preempted
worker's unfinished items are re-dispatched. On Postgres the claim uses ``FOR UPDATE SKIP LOCKED``;
on SQLite (single coordinator process) an in-process lock serializes claims for the same guarantee.
"""

from __future__ import annotations

import json
import threading

from sqlalchemy import func, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .. import metrics
from ..config import CoordinatorConfig
from ..db import is_postgres
from ..models import Batch, Counter, RequestItem, Worker, now
from ..schemas import (
    CompleteRequest,
    CompleteResponse,
    HeartbeatRequest,
    HeartbeatResponse,
    LeaseItem,
    LeaseRequest,
    LeaseResponse,
)


class Queue:
    def __init__(self, engine: Engine, Session: sessionmaker[Session], cfg: CoordinatorConfig):
        self.engine = engine
        self.Session = Session
        self.cfg = cfg
        self._postgres = is_postgres(engine)
        self._claim_lock = threading.Lock()  # only used on SQLite

    def _lease_duration(self, requested: int) -> int:
        """Lease length is a coordinator policy: clamp the worker's ask to our max.

        A negative value is an explicit release (drain) and passes through so the items expire
        immediately and the reaper re-dispatches them on its next pass.
        """
        if requested < 0:
            return requested
        return min(requested, self.cfg.lease_seconds)

    # ---- worker bookkeeping --------------------------------------------------------------------
    def _touch_worker(self, s: Session, worker_id: str, engine: str, model: str) -> None:
        w = s.get(Worker, worker_id)
        if w is None:
            s.add(Worker(id=worker_id, engine=engine, model=model))
        else:
            w.last_seen = now()
            w.engine = engine or w.engine
            w.model = model or w.model

    def _bump_counter(self, s: Session, key: str, amount: float) -> None:
        c = s.get(Counter, key)
        if c is None:
            s.add(Counter(key=key, value=amount))
        else:
            c.value += amount

    # ---- lease ---------------------------------------------------------------------------------
    def claim(self, req: LeaseRequest) -> LeaseResponse:
        max_items = min(req.max_items, self.cfg.max_chunk)
        lock = self._claim_lock if not self._postgres else _NullCtx()
        with lock, self.Session.begin() as s:
            self._touch_worker(s, req.worker_id, req.engine, req.model)
            q = (
                select(RequestItem)
                .join(Batch, Batch.id == RequestItem.batch_id)
                .where(RequestItem.status == "pending", Batch.status == "in_progress")
                # prefix_group first => a worker gets a homogeneous, cache-friendly chunk.
                .order_by(RequestItem.prefix_group, RequestItem.seq)
                .limit(max_items)
            )
            if self._postgres:
                q = q.with_for_update(skip_locked=True, of=RequestItem)
            items = list(s.scalars(q).all())
            expires = now() + self._lease_duration(req.lease_seconds)
            out: list[LeaseItem] = []
            for it in items:
                it.status = "leased"
                it.leased_by = req.worker_id
                it.lease_expires_at = expires
                it.attempts += 1
                out.append(
                    LeaseItem(
                        item_id=it.id,
                        batch_id=it.batch_id,
                        custom_id=it.custom_id,
                        body=json.loads(it.body_json),
                        prefix_group=it.prefix_group,
                    )
                )
            if out:
                metrics.ATTEMPTS.inc(len(out))
        return LeaseResponse(items=out, lease_expires_at=expires if out else None)

    # ---- complete (idempotent) -----------------------------------------------------------------
    def complete(self, req: CompleteRequest) -> tuple[CompleteResponse, list[str]]:
        accepted = 0
        duplicates = 0
        finalized: list[str] = []
        with self.Session.begin() as s:
            self._touch_worker(s, req.worker_id, "", "")
            touched_batches: set[str] = set()
            for r in req.results:
                it = s.get(RequestItem, r.item_id)
                if it is None:
                    continue
                if it.status in ("done", "failed"):
                    duplicates += 1  # idempotent: first completion wins
                    continue
                it.completed_at = now()
                it.prompt_tokens = r.prompt_tokens
                it.output_tokens = r.output_tokens
                it.cache_hit = 1 if r.cache_hit else 0
                if r.error is not None and r.response is None:
                    it.status = "failed"
                    it.error_json = json.dumps(r.error)
                else:
                    it.status = "done"
                    it.response_json = json.dumps(r.response)
                accepted += 1
                touched_batches.add(it.batch_id)

                # metrics
                metrics.PROMPT_TOKENS.inc(r.prompt_tokens)
                metrics.OUTPUT_TOKENS.inc(r.output_tokens)
                if it.status == "done":
                    metrics.REQUESTS_COMPLETED.inc()
                else:
                    metrics.REQUESTS_FAILED.inc()
                if r.cache_hit:
                    metrics.CACHE_HITS.inc()
                else:
                    metrics.CACHE_MISSES.inc()

            for batch_id in touched_batches:
                if self._recount_batch(s, batch_id):
                    finalized.append(batch_id)
        return CompleteResponse(accepted=accepted, duplicates=duplicates), finalized

    def _recount_batch(self, s: Session, batch_id: str) -> bool:
        """Refresh a batch's counts; flip to ``finalizing`` when all items are terminal.

        Returns True if the batch just became ready for output assembly.
        """
        b = s.get(Batch, batch_id)
        if b is None:
            return False
        completed = s.scalar(
            select(func.count())
            .select_from(RequestItem)
            .where(RequestItem.batch_id == batch_id, RequestItem.status == "done")
        )
        failed = s.scalar(
            select(func.count())
            .select_from(RequestItem)
            .where(RequestItem.batch_id == batch_id, RequestItem.status == "failed")
        )
        b.completed = int(completed or 0)
        b.failed = int(failed or 0)
        if b.status == "in_progress" and (b.completed + b.failed) >= b.total:
            b.status = "finalizing"
            b.finalizing_at = now()
            return True
        return False

    # ---- heartbeat -----------------------------------------------------------------------------
    def heartbeat(self, req: HeartbeatRequest) -> HeartbeatResponse:
        extended = 0
        revoked: list[str] = []
        expires = now() + self._lease_duration(req.lease_seconds)
        with self.Session.begin() as s:
            self._touch_worker(s, req.worker_id, req.engine, req.model)
            for item_id in req.holding:
                it = s.get(RequestItem, item_id)
                if it is None:
                    continue
                if it.status == "leased" and it.leased_by == req.worker_id:
                    it.lease_expires_at = expires
                    extended += 1
                else:
                    # Reclaimed and possibly reassigned, or already completed elsewhere.
                    revoked.append(item_id)
        return HeartbeatResponse(ok=True, extended=extended, revoked=revoked)

    # ---- reaper --------------------------------------------------------------------------------
    def reap(self) -> int:
        """Reclaim expired leases. Returns how many were reclaimed this pass."""
        t = now()
        reclaimed = 0
        with self.Session.begin() as s:
            expired = list(
                s.scalars(
                    select(RequestItem).where(
                        RequestItem.status == "leased",
                        RequestItem.lease_expires_at.is_not(None),
                        RequestItem.lease_expires_at < t,
                    )
                ).all()
            )
            for it in expired:
                if it.attempts >= self.cfg.max_attempts:
                    it.status = "failed"
                    it.error_json = json.dumps(
                        {"message": f"exhausted {it.attempts} attempts", "type": "max_attempts"}
                    )
                    it.completed_at = t
                    metrics.REQUESTS_FAILED.inc()
                    self._recount_batch(s, it.batch_id)
                else:
                    it.status = "pending"
                    it.leased_by = None
                    it.lease_expires_at = None
                reclaimed += 1
            if reclaimed:
                self._bump_counter(s, "leases_reclaimed", reclaimed)
                metrics.LEASES_RECLAIMED.inc(reclaimed)
        return reclaimed

    # ---- gauges / stats ------------------------------------------------------------------------
    def refresh_gauges(self) -> dict[str, float]:
        t = now()
        with self.Session.begin() as s:
            pending = s.scalar(
                select(func.count()).select_from(RequestItem).where(RequestItem.status == "pending")
            )
            leased = s.scalar(
                select(func.count()).select_from(RequestItem).where(RequestItem.status == "leased")
            )
            active_workers = s.scalar(
                select(func.count())
                .select_from(Worker)
                .where(Worker.last_seen > t - self.cfg.worker_ttl)
            )
            # Cost estimate: aggregate GPU-uptime across workers seen recently * spot price.
            uptime = func.coalesce(func.sum(Worker.last_seen - Worker.first_seen), 0.0)
            worker_seconds = (
                s.scalar(
                    select(uptime).where(Worker.last_seen > t - self.cfg.worker_ttl)
                )
                or 0.0
            )
        cost = (float(worker_seconds) / 3600.0) * self.cfg.spot_gpu_hourly_usd
        metrics.QUEUE_PENDING.set(int(pending or 0))
        metrics.QUEUE_LEASED.set(int(leased or 0))
        metrics.ACTIVE_WORKERS.set(int(active_workers or 0))
        metrics.COST_USD.set(cost)
        return {
            "pending": int(pending or 0),
            "leased": int(leased or 0),
            "active_workers": int(active_workers or 0),
            "estimated_cost_usd": cost,
        }


class _NullCtx:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def reset_stuck_on_startup(Session: sessionmaker[Session]) -> int:
    """On coordinator restart, return any still-leased items to pending."""
    with Session.begin() as s:
        result = s.execute(
            update(RequestItem)
            .where(RequestItem.status == "leased")
            .values(status="pending", leased_by=None, lease_expires_at=None)
        )
        return result.rowcount or 0
