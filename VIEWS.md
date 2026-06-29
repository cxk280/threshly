# VIEWS

Threshly's surface area. The MVP is a **CLI + HTTP API + metrics**; there is no web UI yet (a
dashboard is a later, separately-designed phase — see the bottom of this file).

## CLI views (`threshly …`)

Each command's terminal output is a "view".

### `threshly coordinator`
Starts the control-plane server. Output: a startup banner showing bind address, database URL
(redacted), blob-store backend, and the metrics endpoint, followed by a structured request log.

### `threshly worker --coordinator <url> --engine <mock|vllm> [--model <name>]`
Starts a worker. Output: a startup line (worker id, engine, model, coordinator), then a periodic
status line — items leased, completed, in-flight, current req/s and tok/s. On SIGTERM, prints a
drain summary (results flushed, lease released) before exiting cleanly.

### `threshly submit <input.jsonl> --model <name> [--endpoint <chat|...>]`
Submits a batch. Output: the created **batch object** (id, status `validating|in_progress`,
request counts, created_at) as pretty JSON, and the bare batch id on the last line for scripting.

### `threshly status <batch-id>`
One-shot status. Output: the batch object — status, `request_counts` (total/completed/failed),
timing, and any top-level error.

### `threshly watch <batch-id>`
Live progress view (Rich). A refreshing panel showing: a progress bar (completed/total), failed
count, active workers, throughput (req/s, output tok/s), prefix-cache hit rate, preemptions
survived, elapsed time, and estimated cost. Exits when the batch reaches a terminal state.

### `threshly results <batch-id> [-o out.jsonl]`
Downloads results. Output: writes the output JSONL (one result object per input `custom_id`) to the
path (or stdout), then prints a one-line summary (n results, n errors, output file path).

### `threshly cancel <batch-id>`
Cancels a batch. Output: the updated batch object with status `cancelling|cancelled`.

## HTTP API views (coordinator)

OpenAI-Batch-compatible JSON responses:

- `POST /v1/files` (`purpose=batch`) → **file object** (id, bytes, created_at, filename, purpose).
- `POST /v1/batches` → **batch object**.
- `GET /v1/batches/{id}` → **batch object**.
- `GET /v1/batches` → list of batch objects.
- `POST /v1/batches/{id}/cancel` → **batch object** (status `cancelling`).
- `GET /v1/files/{id}/content` → raw JSONL (output or error file).
- `GET /healthz` → `{"status": "ok"}`.
- `GET /metrics` → Prometheus exposition text (throughput, prefix-cache hit rate, active workers,
  queue depth, preemptions survived, retries, cost estimate).

Internal (worker↔coordinator) endpoints — not part of the public surface:
`POST /internal/lease`, `POST /internal/complete`, `POST /internal/heartbeat`.

## Observability views

- **Prometheus `/metrics`** — the metric series above.
- **Grafana dashboard** (`deploy/grafana/`) — panels for throughput, prefix-cache hit rate,
  active workers, queue depth over time, preemptions survived, and running cost estimate.

## Future: Web dashboard (NOT yet designed)

A browser dashboard (batch list, per-batch drill-down, live throughput/cache charts, worker fleet
view) is planned. Per project rules, that UI must be described here in full, mocked in Figma, and
approved **before** any UI coding begins. It is intentionally out of scope for the CLI/API MVP.
