# Threshly

**A self-hosted, OpenAI-Batch-compatible batch inference engine for spot/preemptible GPUs.**

Point Threshly at a JSONL file of (potentially millions of) chat-completion requests. It runs them
across a fleet of **cheap preemptible GPU workers** running [vLLM](https://github.com/vllm-project/vllm),
**survives workers being killed at any moment with no lost or duplicated work**, groups requests by
shared prefix so vLLM's KV cache is **reused instead of recomputed**, and writes a results JSONL.
It speaks the **OpenAI Batch API** shape, so the clients and SDKs you already have keep working.

---

## The 30-second pitch

> Offline LLM jobs — generating synthetic data, running evals, classifying or labeling a backlog,
> enriching documents — don't need millisecond latency. They need **throughput and low cost**.
> The cheapest way to get GPUs is the *spot/preemptible* market (roughly half price), but spot
> instances can be reclaimed mid-job, so almost nobody runs large LLM batches on them — they either
> overpay for on-demand GPUs, or ship their data to a hosted Batch API they can't use for
> privacy/compliance reasons and that only serves the provider's own models.
>
> **Threshly makes spot GPUs safe for batch LLM inference.** Every request is leased to a worker
> with a deadline; if a worker vanishes, the work is automatically re-dispatched, and completion is
> idempotent so nothing is ever done twice. You run your own models, on your own hardware, on the
> cheapest capacity available — and the API looks exactly like the one your code already calls.

It is the **inference-serving / scale layer**: it operates vLLM at non-trivial scale and treats
worker death as the normal case, not an exception.

## Why this is hard (and why it doesn't exist yet)

A naive "loop over a file and call the model" script on a spot instance loses everything in flight
the moment the instance is reclaimed, and has no way to spread work over many GPUs. Doing it
properly requires a small distributed system:

- a **durable work queue** that knows which requests are done, in flight, or waiting;
- **leases with deadlines** so a dead worker's work is provably reclaimable;
- **idempotent completion** so a reclaimed-then-redone request can't be double-counted;
- **prefix-aware scheduling** so sharding across workers doesn't destroy KV-cache locality;
- and an **operable surface** (metrics, cost, an OpenAI-compatible API) so it's usable in practice.

Threshly is that system, packaged so it runs on a laptop with no GPU for development and demos, and
on a real preemptible GPU fleet in production — behind the same interface.

## How it works

```
            ┌──────────────────────────────────────────────┐
  client ──▶│  Coordinator (FastAPI)                        │
  (OpenAI   │  • OpenAI-Batch API  /v1/files /v1/batches    │
   Batch    │  • durable lease queue (SKIP LOCKED)          │
   SDK)     │  • prefix-aware scheduling                    │
            │  • reaper: reclaim expired leases             │
            └───────▲───────────────────────┬───────────────┘
                    │ lease / complete /     │ dispatch
                    │ heartbeat              │ (prefix-grouped)
        ┌───────────┴───┐   ┌───────────┐   ┌┴──────────┐
        │ Worker (spot) │   │ Worker    │   │ Worker    │   …ephemeral, killable
        │  Engine:      │   │  Engine:  │   │  Engine:  │
        │  vLLM | mock  │   │  vLLM     │   │  vLLM     │
        └───────────────┘   └───────────┘   └───────────┘

  state: SQLAlchemy (SQLite → Postgres)   blobs: local FS → S3/MinIO
```

1. **Submit.** You upload an input JSONL (the OpenAI batch line format) and create a batch. The
   coordinator splits it into per-request work items, each tagged with a **prefix group** (a hash of
   its shared leading context — typically a system prompt or a shared RAG/document block).

2. **Lease.** Workers pull a chunk of pending items. The dispatch query orders by `(prefix_group,
   seq)`, so each worker gets a **cache-homogeneous chunk** — items that share a prefix. With vLLM's
   automatic prefix caching enabled, the KV cache for that shared prefix is computed once and reused
   across the whole chunk instead of recomputed per request.

3. **Heartbeat & deadline.** Each lease has a TTL set by the coordinator (workers can request a
   shorter one but never a longer one — lease length is *coordinator policy*). Workers heartbeat to
   extend leases while they make progress.

4. **Survive preemption.** Two paths, both safe:
   - *Graceful (SIGTERM, the spot ~30–120s warning):* the worker submits whatever it has finished
     and **releases the rest of its lease immediately**, so it's re-dispatched within one reaper tick.
   - *Hard (instance vanishes / SIGKILL):* the lease simply expires; the **reaper** reclaims those
     items and returns them to the queue.
   Because completion is **idempotent per request id**, a request that was reclaimed and redone — or
   a late result from a zombie worker — is counted exactly once.

5. **Finalize.** When every item is terminal, the coordinator assembles the output JSONL (one result
   per `custom_id`, in OpenAI's `{custom_id, response, error}` shape) and an error file for failures.

## What you can see it do (verified end-to-end)

Running locally with the GPU-free mock engine and two workers, submitting a 600-request batch and
**`kill -9`-ing a worker mid-run**:

- the killed worker had leased 64 items and finished 32; the reaper reclaimed the **32 unfinished**
  items within the lease cap and the surviving worker picked them up;
- the batch completed **600/600, with exactly 600 unique `custom_id`s** in the output — nothing lost,
  nothing duplicated;
- `/metrics` reported `threshly_leases_reclaimed_total 32` ("preemptions survived"), a live
  prefix-cache hit rate (e.g. 22 hits / 2 misses when all requests share a system prompt — one miss
  per worker, the rest hits), throughput, active workers, queue depth, and a running cost estimate.

## Quickstart — no GPU required

```bash
pip install -e .

# 1. start the coordinator (SQLite + local blob store by default)
threshly coordinator                       # serves http://localhost:8080, metrics at /metrics

# 2. in other shells, start a couple of mock workers (no GPU)
threshly worker --coordinator http://localhost:8080 --engine mock
threshly worker --coordinator http://localhost:8080 --engine mock

# 3. submit a batch, watch it live, fetch results
python examples/gen_sample.py 200 > batch.jsonl
threshly submit batch.jsonl --model demo-model          # prints a batch id
threshly watch  <batch-id>                              # live progress dashboard
threshly results <batch-id> -o out.jsonl
```

Now `kill` one of the workers mid-run — the batch still finishes, exactly once. Try `kill -9` to see
the reaper reclaim its in-flight work.

## Running on real GPUs (vLLM)

```bash
pip install -e ".[vllm]"
threshly worker --coordinator http://coordinator:8080 \
  --engine vllm --model meta-llama/Llama-3.1-8B-Instruct
```

The vLLM engine turns on automatic prefix caching and high `max-num-seqs` batching, and processes
each leased chunk in a single batched `chat` call — so the prefix-grouped chunks the scheduler hands
out translate directly into KV-cache reuse on the GPU. Run as many workers as you have GPUs;
preemptible/spot instances are the intended target. The mock and vLLM engines sit behind the same
`Engine` interface, so everything above is identical.

## The OpenAI-compatible API

| Method & path | Purpose |
|---|---|
| `POST /v1/files` (`purpose=batch`) | Upload an input JSONL; returns a file object. |
| `POST /v1/batches` | Create a batch from an uploaded file. |
| `GET /v1/batches/{id}` | Batch object with `request_counts` and status. |
| `GET /v1/batches` | List recent batches. |
| `POST /v1/batches/{id}/cancel` | Cancel a batch. |
| `GET /v1/files/{id}/content` | Download output or error JSONL. |
| `GET /metrics` | Prometheus metrics. |
| `GET /healthz` | Liveness. |

Input line format (same as OpenAI's Batch API):

```json
{"custom_id": "req-1", "method": "POST", "url": "/v1/chat/completions",
 "body": {"model": "demo-model", "messages": [{"role": "user", "content": "hello"}]}}
```

Output line format:

```json
{"id": "...", "custom_id": "req-1",
 "response": {"status_code": 200, "body": { /* chat.completion */ }}, "error": null}
```

## Operating it

Prometheus metrics (on the coordinator, also exposed via the live `watch` view):

- `threshly_requests_completed_total`, `threshly_requests_failed_total`
- `threshly_output_tokens_total`, `threshly_prompt_tokens_total`
- `threshly_prefix_cache_hits_total`, `threshly_prefix_cache_misses_total` → cache hit rate
- `threshly_active_workers`, `threshly_queue_pending`, `threshly_queue_leased`
- `threshly_leases_reclaimed_total` → **preemptions survived**
- `threshly_estimated_cost_usd` → spot-price-aware running cost

## Configuration

Everything has a working default; override via environment variables.

| Variable | Default | Meaning |
|---|---|---|
| `THRESHLY_DATABASE_URL` | `sqlite:///threshly.db` | State store; use a Postgres URL for a cluster. |
| `THRESHLY_BLOB_DIR` / `THRESHLY_S3_BUCKET` | `./threshly_data` | Where input/output JSONL live (local FS or S3/MinIO). |
| `THRESHLY_LEASE_SECONDS` | `60` | Max lease TTL — the coordinator's reclaim deadline. |
| `THRESHLY_REAPER_INTERVAL` | `5` | How often expired leases are reclaimed. |
| `THRESHLY_MAX_CHUNK` | `32` | Max items leased per request. |
| `THRESHLY_MAX_ATTEMPTS` | `5` | Deliveries before an item is marked failed. |
| `THRESHLY_SPOT_GPU_HOURLY_USD` | `0.50` | Spot price used for the cost estimate. |

## Design choices worth noting

- **The coordinator owns the lease deadline.** Workers may request a shorter lease but never a
  longer one, so a vanished worker's items are always reclaimable within a bounded time — you can't
  accidentally strand work by misconfiguring a worker.
- **Postgres-grade concurrency, SQLite-grade setup.** On Postgres the claim uses `FOR UPDATE SKIP
  LOCKED`; on SQLite (single coordinator) an in-process lock gives the same exclusivity. No Redis,
  no Celery, no broker to operate.
- **Idempotency is the safety net, not retries.** Exactly-once output comes from idempotent
  completion keyed on request id, which makes aggressive re-dispatch safe rather than dangerous.

## Status & scope

Active development; MIT licensed. The current surface is a **CLI + HTTP API + metrics** (see
`VIEWS.md`). A browser dashboard is planned but intentionally out of scope until its views are
specified and approved. Contributions and issues welcome.
