from __future__ import annotations

import json

import pytest

from threshly.config import CoordinatorConfig
from threshly.coordinator.app import Coordinator
from threshly.schemas import CompletedResult, CompleteRequest, CreateBatchIn, LeaseRequest
from threshly.worker.engines.base import EngineRequest
from threshly.worker.engines.mock import MockEngine


@pytest.fixture()
def cfg(tmp_path):
    return CoordinatorConfig(
        database_url=f"sqlite:///{tmp_path/'t.db'}",
        blob_dir=str(tmp_path / "blobs"),
        lease_seconds=1,
        reaper_interval=0.1,
        worker_ttl=30,
        max_attempts=5,
    )


@pytest.fixture()
def coord(cfg):
    return Coordinator(cfg)


def make_input(n: int = 12, shared_system: bool = True) -> bytes:
    lines = []
    for i in range(n):
        system = "shared system prompt" if shared_system else f"system-{i}"
        lines.append(
            json.dumps(
                {
                    "custom_id": f"request-{i}",
                    "body": {
                        "model": "demo-model",
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": f"text number {i}"},
                        ],
                    },
                }
            )
        )
    return ("\n".join(lines) + "\n").encode()


def submit_batch(coord: Coordinator, n: int = 12, shared_system: bool = True) -> str:
    f = coord.save_file("in.jsonl", "batch", make_input(n, shared_system))
    batch = coord.create_batch(CreateBatchIn(input_file_id=f.id))
    return batch.id


def drain_via_worker(coord: Coordinator, worker_id: str = "wkr-test", chunk: int = 4) -> None:
    """Run a synchronous mock 'worker' until no work remains."""
    engine = MockEngine(latency_s=0)
    while True:
        lease = coord.queue.claim(
            LeaseRequest(worker_id=worker_id, max_items=chunk, lease_seconds=60)
        )
        if not lease.items:
            break
        reqs = [
            EngineRequest(item_id=i.item_id, custom_id=i.custom_id, body=i.body,
                          prefix_group=i.prefix_group)
            for i in lease.items
        ]
        results = engine.generate(reqs)
        coord.queue.complete(
            CompleteRequest(
                worker_id=worker_id,
                results=[
                    CompletedResult(
                        item_id=r.item_id, response=r.response, error=r.error,
                        prompt_tokens=r.prompt_tokens, output_tokens=r.output_tokens,
                        cache_hit=r.cache_hit,
                    )
                    for r in results
                ],
            )
        )
    coord.finalize_pending()
