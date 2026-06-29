from __future__ import annotations

import json
import time

from conftest import submit_batch

from threshly.schemas import CompletedResult, CompleteRequest, LeaseRequest


def test_expired_lease_is_reclaimed_and_completes_once(coord):
    """Simulate a spot preemption: worker A leases, dies; the reaper reclaims; B finishes."""
    bid = submit_batch(coord, n=8)

    # Worker A leases everything, completes half, then "dies" (never completes the rest).
    a = coord.queue.claim(LeaseRequest(worker_id="A", max_items=8, lease_seconds=1))
    assert len(a.items) == 8
    half = a.items[:4]
    coord.queue.complete(
        CompleteRequest(
            worker_id="A",
            results=[CompletedResult(item_id=i.item_id, response={"ok": 1}) for i in half],
        )
    )

    # Before expiry, the remaining items are not re-leasable.
    mid = coord.queue.claim(LeaseRequest(worker_id="B", max_items=8, lease_seconds=60))
    assert mid.items == []

    # Lease expires; reaper reclaims the 4 unfinished items.
    time.sleep(1.1)
    reclaimed = coord.queue.reap()
    assert reclaimed == 4

    # Worker B picks them up and finishes.
    b = coord.queue.claim(LeaseRequest(worker_id="B", max_items=8, lease_seconds=60))
    assert len(b.items) == 4
    coord.queue.complete(
        CompleteRequest(
            worker_id="B",
            results=[CompletedResult(item_id=i.item_id, response={"ok": 1}) for i in b.items],
        )
    )
    coord.finalize_pending()

    batch = coord.get_batch(bid)
    assert batch.status == "completed"
    assert batch.request_counts.completed == 8  # exactly once, nothing lost or duplicated
    out = coord.file_content(batch.output_file_id).decode()
    custom_ids = [json.loads(x)["custom_id"] for x in out.splitlines() if x.strip()]
    assert sorted(custom_ids) == sorted({f"request-{i}" for i in range(8)})


def test_coordinator_clamps_worker_lease(coord):
    """Lease length is coordinator policy: a worker asking for a long lease is clamped to the max.

    This stops a vanished worker's items from being stuck (un-reclaimable) for the worker's ask.
    """
    submit_batch(coord, n=4)
    # coord fixture sets lease_seconds=1; worker asks for 999.
    coord.queue.claim(LeaseRequest(worker_id="greedy", max_items=4, lease_seconds=999))
    import time as _t

    _t.sleep(1.1)  # past the coordinator's 1s cap, well under the worker's 999s ask
    assert coord.queue.reap() == 4  # reclaimed despite the worker's long request


def test_late_result_from_preempted_worker_is_idempotent(coord):
    """A zombie worker's late completion must not double-count after reclaim + redo."""
    submit_batch(coord, n=4)
    a = coord.queue.claim(LeaseRequest(worker_id="A", max_items=4, lease_seconds=1))
    time.sleep(1.1)
    coord.queue.reap()  # back to pending
    b = coord.queue.claim(LeaseRequest(worker_id="B", max_items=4, lease_seconds=60))
    coord.queue.complete(
        CompleteRequest(
            worker_id="B",
            results=[CompletedResult(item_id=i.item_id, response={"by": "B"}) for i in b.items],
        )
    )
    # Now the zombie A reports its old results — must be treated as duplicates.
    resp, _ = coord.queue.complete(
        CompleteRequest(
            worker_id="A",
            results=[CompletedResult(item_id=i.item_id, response={"by": "A"}) for i in a.items],
        )
    )
    assert resp.accepted == 0
    assert resp.duplicates == 4
