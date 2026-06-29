# Deploying Threshly

The Compose stack runs the whole system the way you'd run it in production — Postgres for state,
MinIO (S3) for blobs, and Prometheus + Grafana for observability — but on your laptop, with the
GPU-free mock engine so it works anywhere.

## Bring up the stack

```bash
docker compose -f deploy/docker-compose.yml up --build
# or scale the worker fleet:
docker compose -f deploy/docker-compose.yml up --build --scale worker=5
```

| Service | URL | Notes |
|---|---|---|
| Coordinator | http://localhost:8080 | OpenAI-Batch API + `/metrics` |
| Prometheus | http://localhost:9090 | scrapes the coordinator every 2s |
| Grafana | http://localhost:3000 | anonymous admin; "Threshly — Batch Inference" dashboard auto-loads |
| MinIO console | http://localhost:9001 | `minioadmin` / `minioadmin` |

## Run a batch against it

From the host (with `pip install -e .` in a venv):

```bash
python examples/gen_sample.py 2000 > batch.jsonl
threshly submit batch.jsonl --model demo-model        # -> batch id
threshly watch <batch-id>
```

Now watch the Grafana dashboard while you **kill a worker** to see preemption recovery live:

```bash
docker compose -f deploy/docker-compose.yml kill -s SIGKILL <one worker container>
```

The `threshly_leases_reclaimed_total` ("preemptions survived") panel ticks up, the queue drains,
and the batch still completes exactly once.

## Going to real GPUs

The image here installs the `postgres` and `s3` extras but **not** vLLM. For GPU workers, build a
CUDA-based image that runs `pip install "threshly[vllm]"` and start it with:

```bash
threshly worker --coordinator http://<coordinator>:8080 --engine vllm --model <hf-model>
```

Point it at the same coordinator (Postgres + S3) and scale workers across your spot/preemptible GPU
instances. Set `THRESHLY_LEASE_SECONDS` to a value comfortably above your longest expected chunk
runtime so healthy workers aren't reclaimed mid-chunk, but low enough that a vanished instance's
work is re-dispatched promptly.
