from __future__ import annotations

import json

from conftest import drain_via_worker, submit_batch


def test_batch_completes_exactly_once(coord):
    bid = submit_batch(coord, n=12)
    drain_via_worker(coord)

    b = coord.get_batch(bid)
    assert b.status == "completed"
    assert b.request_counts.completed == 12
    assert b.request_counts.failed == 0
    assert b.output_file_id

    out = coord.file_content(b.output_file_id).decode()
    lines = [json.loads(x) for x in out.splitlines() if x.strip()]
    assert len(lines) == 12
    # exactly one result per custom_id
    custom_ids = [x["custom_id"] for x in lines]
    assert sorted(custom_ids) == sorted({f"request-{i}" for i in range(12)})
    assert all(x["response"]["status_code"] == 200 for x in lines)


def test_prefix_cache_hits_with_shared_system(coord):
    from threshly import metrics

    before_hits = metrics.CACHE_HITS._value.get()
    submit_batch(coord, n=12, shared_system=True)
    drain_via_worker(coord)
    after_hits = metrics.CACHE_HITS._value.get()
    # With a shared system prompt, all but the first item of the group should hit.
    assert after_hits - before_hits >= 10
