"""The ``threshly`` command-line interface."""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

import httpx
import typer
from rich.console import Console
from rich.live import Live
from rich.table import Table

from .config import CoordinatorConfig, WorkerConfig

app = typer.Typer(add_completion=False, help="Threshly — batch inference for spot GPUs.")
console = Console()

TERMINAL = {"completed", "failed", "cancelled", "expired"}


def _setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S"
    )


@app.command()
def coordinator(
    host: str = typer.Option(None, help="Bind host (default 0.0.0.0 / $THRESHLY_HOST)."),
    port: int = typer.Option(None, help="Bind port (default 8080 / $THRESHLY_PORT)."),
) -> None:
    """Run the coordinator (control plane + queue + reaper)."""
    import uvicorn

    from .coordinator.app import create_app

    cfg = CoordinatorConfig()
    _setup_logging()
    h = host or cfg.host
    p = port or cfg.port
    console.print(
        f"[bold green]Threshly coordinator[/] on http://{h}:{p}  "
        f"db=[cyan]{_redact(cfg.database_url)}[/]  "
        f"blobs=[cyan]{'s3:' + cfg.blob_s3_bucket if cfg.blob_s3_bucket else cfg.blob_dir}[/]  "
        f"metrics=http://{h}:{p}/metrics"
    )
    uvicorn.run(create_app(cfg), host=h, port=p, log_level="warning")


@app.command()
def worker(
    coordinator: str = typer.Option(..., help="Coordinator base URL, e.g. http://localhost:8080"),
    engine: str = typer.Option("mock", help="Engine: 'mock' (no GPU) or 'vllm'."),
    model: str = typer.Option("demo-model", help="Model name to serve."),
    lease_size: int = typer.Option(16, help="Items to lease per chunk."),
) -> None:
    """Run a worker. Safe to kill at any time (SIGTERM drains cleanly)."""
    from .worker.runner import run_worker

    _setup_logging()
    cfg = WorkerConfig(
        coordinator_url=coordinator, engine=engine, model=model, lease_size=lease_size
    )
    run_worker(cfg)


@app.command()
def submit(
    input: Path = typer.Argument(..., help="Input JSONL file (OpenAI batch line format)."),
    model: str = typer.Option(None, help="Model to run (else inferred from request bodies)."),
    endpoint: str = typer.Option("/v1/chat/completions", help="Target endpoint."),
    coordinator: str = typer.Option("http://localhost:8080", help="Coordinator base URL."),
) -> None:
    """Upload an input file and create a batch. Prints the batch id on the last line."""
    if not input.exists():
        console.print(f"[red]No such file:[/] {input}")
        raise typer.Exit(1)
    with httpx.Client(base_url=coordinator, timeout=60.0) as c:
        files = {"file": (input.name, input.read_bytes(), "application/jsonl")}
        r = c.post("/v1/files", files=files, data={"purpose": "batch"})
        r.raise_for_status()
        file_id = r.json()["id"]
        payload = {"input_file_id": file_id, "endpoint": endpoint}
        if model:
            payload["model"] = model
        r = c.post("/v1/batches", json=payload)
        r.raise_for_status()
        batch = r.json()
    console.print_json(json.dumps(batch))
    print(batch["id"])


@app.command()
def status(
    batch_id: str = typer.Argument(...),
    coordinator: str = typer.Option("http://localhost:8080"),
) -> None:
    """Show a batch's current status."""
    with httpx.Client(base_url=coordinator, timeout=30.0) as c:
        r = c.get(f"/v1/batches/{batch_id}")
        if r.status_code == 404:
            console.print("[red]batch not found[/]")
            raise typer.Exit(1)
        r.raise_for_status()
    console.print_json(json.dumps(r.json()))


@app.command()
def watch(
    batch_id: str = typer.Argument(...),
    coordinator: str = typer.Option("http://localhost:8080"),
    interval: float = typer.Option(1.0, help="Refresh interval (seconds)."),
) -> None:
    """Live progress view until the batch reaches a terminal state."""
    start = time.time()
    with httpx.Client(base_url=coordinator, timeout=30.0) as c:
        with Live(console=console, refresh_per_second=4) as live:
            while True:
                b = c.get(f"/v1/batches/{batch_id}").json()
                metrics = _scrape_metrics(c)
                live.update(_render(b, metrics, time.time() - start))
                if b.get("status") in TERMINAL:
                    break
                time.sleep(interval)


@app.command()
def results(
    batch_id: str = typer.Argument(...),
    output: Path = typer.Option(None, "-o", "--output", help="Write results JSONL here."),
    coordinator: str = typer.Option("http://localhost:8080"),
) -> None:
    """Download a completed batch's output JSONL."""
    with httpx.Client(base_url=coordinator, timeout=60.0) as c:
        b = c.get(f"/v1/batches/{batch_id}").json()
        out_id = b.get("output_file_id")
        if not out_id:
            console.print(f"[yellow]Batch status is '{b.get('status')}'; no output yet.[/]")
            raise typer.Exit(1)
        data = c.get(f"/v1/files/{out_id}/content").content
        n_err = 0
        if b.get("error_file_id"):
            err = c.get(f"/v1/files/{b['error_file_id']}/content").content
            n_err = len([x for x in err.splitlines() if x.strip()])
    n = len([x for x in data.splitlines() if x.strip()])
    if output:
        output.write_bytes(data)
        console.print(f"[green]{n} results[/] ({n_err} errors) -> {output}")
    else:
        sys.stdout.buffer.write(data)


@app.command()
def cancel(
    batch_id: str = typer.Argument(...),
    coordinator: str = typer.Option("http://localhost:8080"),
) -> None:
    """Cancel a batch."""
    with httpx.Client(base_url=coordinator, timeout=30.0) as c:
        r = c.post(f"/v1/batches/{batch_id}/cancel")
        r.raise_for_status()
    console.print_json(json.dumps(r.json()))


# ---- helpers -----------------------------------------------------------------------------------
def _redact(url: str) -> str:
    if "@" in url and "//" in url:
        scheme, rest = url.split("//", 1)
        if "@" in rest:
            return f"{scheme}//***@{rest.split('@', 1)[1]}"
    return url


def _scrape_metrics(c: httpx.Client) -> dict[str, float]:
    out: dict[str, float] = {}
    try:
        text = c.get("/metrics").text
    except Exception:
        return out
    wanted = {
        "threshly_active_workers",
        "threshly_queue_pending",
        "threshly_queue_leased",
        "threshly_leases_reclaimed_total",
        "threshly_prefix_cache_hits_total",
        "threshly_prefix_cache_misses_total",
        "threshly_output_tokens_total",
        "threshly_estimated_cost_usd",
    }
    for line in text.splitlines():
        if line.startswith("#") or " " not in line:
            continue
        name, _, val = line.partition(" ")
        if name in wanted:
            try:
                out[name] = float(val)
            except ValueError:
                pass
    return out


def _render(batch: dict, m: dict[str, float], elapsed: float) -> Table:
    rc = batch.get("request_counts", {})
    total = rc.get("total", 0) or 0
    done = rc.get("completed", 0) or 0
    failed = rc.get("failed", 0) or 0
    pct = (done + failed) / total * 100 if total else 0.0
    hits = m.get("threshly_prefix_cache_hits_total", 0.0)
    misses = m.get("threshly_prefix_cache_misses_total", 0.0)
    hit_rate = hits / (hits + misses) * 100 if (hits + misses) else 0.0
    tok_per_s = m.get("threshly_output_tokens_total", 0.0) / elapsed if elapsed > 0 else 0.0

    t = Table(title=f"batch {batch.get('id')}  [{batch.get('status')}]", expand=True)
    t.add_column("metric")
    t.add_column("value", justify="right")
    t.add_row("progress", f"{done + failed}/{total}  ({pct:0.1f}%)")
    t.add_row("completed / failed", f"{done} / {failed}")
    t.add_row("active workers", f"{int(m.get('threshly_active_workers', 0))}")
    t.add_row("pending / leased", f"{int(m.get('threshly_queue_pending', 0))} / "
              f"{int(m.get('threshly_queue_leased', 0))}")
    t.add_row("output tok/s", f"{tok_per_s:0.0f}")
    t.add_row("prefix-cache hit rate", f"{hit_rate:0.1f}%")
    t.add_row("preemptions survived", f"{int(m.get('threshly_leases_reclaimed_total', 0))}")
    t.add_row("est. cost (USD)", f"${m.get('threshly_estimated_cost_usd', 0.0):0.4f}")
    t.add_row("elapsed", f"{elapsed:0.1f}s")
    return t


if __name__ == "__main__":
    app()
