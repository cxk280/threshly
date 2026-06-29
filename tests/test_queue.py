from __future__ import annotations

from conftest import submit_batch

from threshly.schemas import CompletedResult, CompleteRequest, LeaseRequest


def test_claim_is_exclusive(coord):
    submit_batch(coord, n=10)
    a = coord.queue.claim(LeaseRequest(worker_id="A", max_items=6, lease_seconds=60))
    b = coord.queue.claim(LeaseRequest(worker_id="B", max_items=6, lease_seconds=60))
    ids_a = {i.item_id for i in a.items}
    ids_b = {i.item_id for i in b.items}
    assert ids_a and ids_b
    assert ids_a.isdisjoint(ids_b)  # no item leased to two workers
    assert len(ids_a) + len(ids_b) <= 10


def test_complete_is_idempotent(coord):
    bid = submit_batch(coord, n=4)
    lease = coord.queue.claim(LeaseRequest(worker_id="A", max_items=4, lease_seconds=60))
    results = [
        CompletedResult(item_id=i.item_id, response={"ok": True}, output_tokens=3)
        for i in lease.items
    ]
    r1, fin1 = coord.queue.complete(CompleteRequest(worker_id="A", results=results))
    r2, fin2 = coord.queue.complete(CompleteRequest(worker_id="A", results=results))

    assert r1.accepted == 4 and r1.duplicates == 0
    assert r2.accepted == 0 and r2.duplicates == 4  # replays are no-ops
    b = coord.get_batch(bid)
    assert b.request_counts.completed == 4  # not double-counted


def test_prefix_grouped_chunk_is_homogeneous(coord):
    # Two distinct system prompts, interleaved; a single lease should be one group, not a mix.
    import json

    lines = []
    for i in range(20):
        system = "GROUP-A" if i % 2 == 0 else "GROUP-B"
        lines.append(
            json.dumps(
                {
                    "custom_id": f"r{i}",
                    "body": {
                        "model": "demo-model",
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": f"u{i}"},
                        ],
                    },
                }
            )
        )
    f = coord.save_file("in.jsonl", "batch", ("\n".join(lines)).encode())
    from threshly.schemas import CreateBatchIn

    coord.create_batch(CreateBatchIn(input_file_id=f.id))
    lease = coord.queue.claim(LeaseRequest(worker_id="A", max_items=5, lease_seconds=60))
    groups = {i.prefix_group for i in lease.items}
    assert len(groups) == 1  # the chunk is cache-homogeneous
