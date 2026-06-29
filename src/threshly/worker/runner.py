"""The worker loop: lease -> run engine -> complete, with heartbeats and a SIGTERM drain.

Designed to be killed at any moment (spot preemption). On SIGTERM it stops taking new work, submits
whatever it has already computed (idempotent on the coordinator), and force-expires the rest of its
lease so the reaper re-dispatches it within one reaper interval — no waiting for the full lease TTL.
"""

from __future__ import annotations

import logging
import signal
import threading
import time

import httpx

from ..config import WorkerConfig
from ..ids import worker_id as new_worker_id
from ..schemas import (
    CompletedResult,
    CompleteRequest,
    HeartbeatRequest,
    LeaseRequest,
    LeaseResponse,
)
from .engines import make_engine
from .engines.base import EngineRequest

log = logging.getLogger("threshly.worker")


class Worker:
    def __init__(self, cfg: WorkerConfig, engine_kwargs: dict | None = None):
        self.cfg = cfg
        self.id = new_worker_id(cfg.engine)
        self.engine = make_engine(cfg.engine, cfg.model, **(engine_kwargs or {}))
        self.client = httpx.Client(base_url=cfg.coordinator_url, timeout=30.0)
        self._draining = threading.Event()
        self._held_lock = threading.Lock()
        self._held: set[str] = set()  # leased item ids not yet completed
        self.completed = 0

    # ---- lifecycle -----------------------------------------------------------------------------
    def install_signal_handlers(self) -> None:
        def handler(signum, _frame):
            log.info("worker %s received signal %s; draining", self.id, signum)
            self._draining.set()

        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)

    def run(self) -> None:
        log.info(
            "worker %s up (engine=%s model=%s coordinator=%s)",
            self.id,
            self.cfg.engine,
            self.cfg.model,
            self.cfg.coordinator_url,
        )
        hb = threading.Thread(target=self._heartbeat_loop, daemon=True)
        hb.start()
        try:
            while not self._draining.is_set():
                self._tick()
        finally:
            self._drain()
        log.info("worker %s exited cleanly (completed=%d)", self.id, self.completed)

    # ---- main step -----------------------------------------------------------------------------
    def _tick(self) -> None:
        lease = self._lease()
        if not lease.items:
            time.sleep(self.cfg.poll_idle_seconds)
            return

        with self._held_lock:
            self._held.update(i.item_id for i in lease.items)

        reqs = [
            EngineRequest(
                item_id=i.item_id,
                custom_id=i.custom_id,
                body=i.body,
                prefix_group=i.prefix_group,
            )
            for i in lease.items
        ]

        # Run the chunk. If preemption arrives mid-chunk, we still submit whatever finished.
        results = self.engine.generate(reqs)
        completed = [
            CompletedResult(
                item_id=r.item_id,
                response=r.response,
                error=r.error,
                prompt_tokens=r.prompt_tokens,
                output_tokens=r.output_tokens,
                cache_hit=r.cache_hit,
            )
            for r in results
        ]
        self._submit(completed)

    def _lease(self) -> LeaseResponse:
        try:
            resp = self.client.post(
                "/internal/lease",
                json=LeaseRequest(
                    worker_id=self.id,
                    engine=self.cfg.engine,
                    model=self.cfg.model,
                    max_items=self.cfg.lease_size,
                    lease_seconds=self.cfg.lease_seconds,
                ).model_dump(),
            )
            resp.raise_for_status()
            return LeaseResponse.model_validate(resp.json())
        except Exception as e:  # transient coordinator hiccup: back off briefly
            log.warning("lease failed: %s", e)
            time.sleep(self.cfg.poll_idle_seconds)
            return LeaseResponse(items=[])

    def _submit(self, results: list[CompletedResult]) -> None:
        if not results:
            return
        try:
            resp = self.client.post(
                "/internal/complete",
                json=CompleteRequest(worker_id=self.id, results=results).model_dump(),
            )
            resp.raise_for_status()
            self.completed += len(results)
            with self._held_lock:
                for r in results:
                    self._held.discard(r.item_id)
        except Exception as e:
            log.warning(
                "complete failed (%d results will be retried via reaper): %s", len(results), e
            )

    # ---- heartbeat -----------------------------------------------------------------------------
    def _heartbeat_loop(self) -> None:
        while not self._draining.is_set():
            time.sleep(self.cfg.heartbeat_interval)
            self._heartbeat(self.cfg.lease_seconds)

    def _heartbeat(self, lease_seconds: int) -> None:
        with self._held_lock:
            holding = list(self._held)
        if not holding and lease_seconds >= 0:
            return
        try:
            resp = self.client.post(
                "/internal/heartbeat",
                json=HeartbeatRequest(
                    worker_id=self.id,
                    engine=self.cfg.engine,
                    model=self.cfg.model,
                    holding=holding,
                    lease_seconds=lease_seconds,
                ).model_dump(),
            )
            resp.raise_for_status()
            revoked = resp.json().get("revoked", [])
            if revoked:
                with self._held_lock:
                    for item_id in revoked:
                        self._held.discard(item_id)
        except Exception as e:
            log.debug("heartbeat failed: %s", e)

    # ---- drain ---------------------------------------------------------------------------------
    def _drain(self) -> None:
        # Force-expire any still-held lease so the reaper re-dispatches it promptly.
        with self._held_lock:
            remaining = list(self._held)
        if remaining:
            log.info("worker %s releasing %d unfinished items on drain", self.id, len(remaining))
            self._heartbeat(lease_seconds=-1)
        self.client.close()


def run_worker(cfg: WorkerConfig, engine_kwargs: dict | None = None) -> None:
    w = Worker(cfg, engine_kwargs=engine_kwargs)
    w.install_signal_handlers()
    w.run()
