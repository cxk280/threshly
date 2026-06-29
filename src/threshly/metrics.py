"""Prometheus metric definitions shared by the coordinator and workers."""

from __future__ import annotations

from prometheus_client import Counter, Gauge

# Throughput.
REQUESTS_COMPLETED = Counter(
    "threshly_requests_completed_total", "Request items completed successfully."
)
REQUESTS_FAILED = Counter("threshly_requests_failed_total", "Request items that failed terminally.")
OUTPUT_TOKENS = Counter("threshly_output_tokens_total", "Output tokens generated.")
PROMPT_TOKENS = Counter("threshly_prompt_tokens_total", "Prompt tokens processed.")

# Prefix caching.
CACHE_HITS = Counter("threshly_prefix_cache_hits_total", "Items served with a prefix-cache hit.")
CACHE_MISSES = Counter("threshly_prefix_cache_misses_total", "Items served without a cache hit.")

# Fleet + queue.
ACTIVE_WORKERS = Gauge("threshly_active_workers", "Workers seen within the worker TTL.")
QUEUE_PENDING = Gauge("threshly_queue_pending", "Request items waiting to be leased.")
QUEUE_LEASED = Gauge("threshly_queue_leased", "Request items currently leased to a worker.")

# Resilience.
LEASES_RECLAIMED = Counter(
    "threshly_leases_reclaimed_total",
    "Expired leases reclaimed by the reaper (preemptions survived).",
)
ATTEMPTS = Counter(
    "threshly_delivery_attempts_total", "Total item delivery attempts (including retries)."
)

# Cost.
COST_USD = Gauge("threshly_estimated_cost_usd", "Spot-price-aware estimated cost so far (USD).")
