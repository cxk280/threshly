"""The coordinator: OpenAI-Batch-compatible control plane + durable queue + reaper."""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import FastAPI, Form, HTTPException, Query, Response, UploadFile
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import select

from .. import ids
from ..config import CoordinatorConfig
from ..db import init_db, make_engine
from ..models import Batch, FileObject, RequestItem, now
from ..schemas import (
    BatchOut,
    CompleteRequest,
    CompleteResponse,
    CreateBatchIn,
    FileObjectOut,
    HeartbeatRequest,
    HeartbeatResponse,
    LeaseRequest,
    LeaseResponse,
    RequestCounts,
)
from ..store import make_store
from .queue import Queue, reset_stuck_on_startup
from .scheduling import prefix_group

log = logging.getLogger("threshly.coordinator")


class Coordinator:
    def __init__(self, cfg: CoordinatorConfig):
        self.cfg = cfg
        self.engine = make_engine(cfg.database_url)
        self.Session = init_db(self.engine)
        self.store = make_store(cfg.blob_dir, cfg.blob_s3_bucket)
        self.queue = Queue(self.engine, self.Session, cfg)
        recovered = reset_stuck_on_startup(self.Session)
        if recovered:
            log.info("recovered %d leased items to pending on startup", recovered)

    # ---- files ---------------------------------------------------------------------------------
    def save_file(self, filename: str, purpose: str, data: bytes) -> FileObjectOut:
        fid = ids.file_id()
        key = f"files/{fid}.jsonl"
        self.store.put(key, data)
        with self.Session.begin() as s:
            s.add(
                FileObject(
                    id=fid, filename=filename, purpose=purpose, bytes=len(data), blob_key=key
                )
            )
        return FileObjectOut(
            id=fid, bytes=len(data), created_at=int(now()), filename=filename, purpose=purpose
        )

    def file_content(self, file_id: str) -> bytes:
        with self.Session.begin() as s:
            f = s.get(FileObject, file_id)
            if f is None:
                raise KeyError(file_id)
            key = f.blob_key
        return self.store.get(key)

    # ---- batches -------------------------------------------------------------------------------
    def create_batch(self, req: CreateBatchIn) -> BatchOut:
        with self.Session.begin() as s:
            f = s.get(FileObject, req.input_file_id)
            if f is None:
                raise KeyError(req.input_file_id)
            blob_key = f.blob_key
        raw = self.store.get(blob_key)

        bid = ids.batch_id()
        items: list[RequestItem] = []
        model = req.model
        seq = 0
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            body = obj.get("body", obj)  # tolerate bare bodies
            custom_id = obj.get("custom_id") or f"request-{seq}"
            if model is None:
                model = body.get("model", "unknown")
            items.append(
                RequestItem(
                    id=ids.item_id(),
                    batch_id=bid,
                    custom_id=custom_id,
                    seq=seq,
                    prefix_group=prefix_group(body, model or "unknown"),
                    body_json=json.dumps(body),
                )
            )
            seq += 1

        if not items:
            raise ValueError("input file contained no requests")

        with self.Session.begin() as s:
            b = Batch(
                id=bid,
                input_file_id=req.input_file_id,
                endpoint=req.endpoint,
                model=model or "unknown",
                completion_window=req.completion_window,
                status="in_progress",
                total=len(items),
                metadata_json=json.dumps(req.metadata) if req.metadata else None,
                in_progress_at=now(),
            )
            s.add(b)
            s.add_all(items)
        return self.get_batch(bid)

    def get_batch(self, batch_id: str) -> BatchOut:
        with self.Session.begin() as s:
            b = s.get(Batch, batch_id)
            if b is None:
                raise KeyError(batch_id)
            return _to_batch_out(b)

    def list_batches(self, limit: int) -> list[BatchOut]:
        with self.Session.begin() as s:
            rows = list(
                s.scalars(select(Batch).order_by(Batch.created_at.desc()).limit(limit)).all()
            )
            return [_to_batch_out(b) for b in rows]

    def cancel_batch(self, batch_id: str) -> BatchOut:
        with self.Session.begin() as s:
            b = s.get(Batch, batch_id)
            if b is None:
                raise KeyError(batch_id)
            if b.status in ("completed", "failed", "cancelled"):
                return _to_batch_out(b)
            b.status = "cancelled"
            b.cancelled_at = now()
            # Pending items will no longer be dispatched (claim filters on in_progress).
        return self.get_batch(batch_id)

    def finalize_batch(self, batch_id: str) -> None:
        """Assemble output/error JSONL files and flip the batch to ``completed``."""
        with self.Session.begin() as s:
            b = s.get(Batch, batch_id)
            if b is None or b.status != "finalizing":
                return
            items = list(
                s.scalars(
                    select(RequestItem)
                    .where(RequestItem.batch_id == batch_id)
                    .order_by(RequestItem.seq)
                ).all()
            )
            out_lines: list[str] = []
            err_lines: list[str] = []
            for it in items:
                if it.status == "done":
                    out_lines.append(
                        json.dumps(
                            {
                                "id": it.id,
                                "custom_id": it.custom_id,
                                "response": {
                                    "status_code": 200,
                                    "body": json.loads(it.response_json)
                                    if it.response_json
                                    else None,
                                },
                                "error": None,
                            }
                        )
                    )
                else:
                    err_lines.append(
                        json.dumps(
                            {
                                "id": it.id,
                                "custom_id": it.custom_id,
                                "response": None,
                                "error": json.loads(it.error_json)
                                if it.error_json
                                else {"message": "unknown error"},
                            }
                        )
                    )

            out_fid = ids.file_id()
            out_key = f"files/{out_fid}.jsonl"
            out_data = ("\n".join(out_lines) + ("\n" if out_lines else "")).encode()
            self.store.put(out_key, out_data)
            s.add(
                FileObject(
                    id=out_fid,
                    filename=f"{batch_id}_output.jsonl",
                    purpose="batch_output",
                    bytes=len(out_data),
                    blob_key=out_key,
                )
            )
            b.output_file_id = out_fid

            if err_lines:
                err_fid = ids.file_id()
                err_key = f"files/{err_fid}.jsonl"
                err_data = ("\n".join(err_lines) + "\n").encode()
                self.store.put(err_key, err_data)
                s.add(
                    FileObject(
                        id=err_fid,
                        filename=f"{batch_id}_errors.jsonl",
                        purpose="batch_error",
                        bytes=len(err_data),
                        blob_key=err_key,
                    )
                )
                b.error_file_id = err_fid

            b.status = "completed"
            b.completed_at = now()
        log.info("batch %s finalized (%d ok, %d failed)", batch_id, b.completed, b.failed)

    def finalize_pending(self) -> None:
        """Finalize any batches sitting in the ``finalizing`` state (idempotent)."""
        with self.Session.begin() as s:
            ids_ = list(
                s.scalars(select(Batch.id).where(Batch.status == "finalizing")).all()
            )
        for bid in ids_:
            self.finalize_batch(bid)


def _to_batch_out(b: Batch) -> BatchOut:
    return BatchOut(
        id=b.id,
        endpoint=b.endpoint,
        input_file_id=b.input_file_id,
        completion_window=b.completion_window,
        status=b.status,
        output_file_id=b.output_file_id,
        error_file_id=b.error_file_id,
        model=b.model,
        created_at=int(b.created_at),
        in_progress_at=int(b.in_progress_at) if b.in_progress_at else None,
        finalizing_at=int(b.finalizing_at) if b.finalizing_at else None,
        completed_at=int(b.completed_at) if b.completed_at else None,
        cancelled_at=int(b.cancelled_at) if b.cancelled_at else None,
        request_counts=RequestCounts(total=b.total, completed=b.completed, failed=b.failed),
        metadata=json.loads(b.metadata_json) if b.metadata_json else None,
        errors={"message": b.error} if b.error else None,
    )


def create_app(cfg: CoordinatorConfig | None = None) -> FastAPI:
    cfg = cfg or CoordinatorConfig()
    coord = Coordinator(cfg)
    app = FastAPI(title="Threshly Coordinator", version="0.1.0")
    app.state.coordinator = coord

    @app.on_event("startup")
    async def _start_reaper() -> None:
        async def loop() -> None:
            while True:
                try:
                    await asyncio.to_thread(coord.queue.reap)
                    await asyncio.to_thread(coord.finalize_pending)
                    await asyncio.to_thread(coord.queue.refresh_gauges)
                except Exception:  # pragma: no cover - keep the loop alive
                    log.exception("reaper iteration failed")
                await asyncio.sleep(cfg.reaper_interval)

        app.state.reaper_task = asyncio.create_task(loop())

    @app.on_event("shutdown")
    async def _stop_reaper() -> None:
        task = getattr(app.state, "reaper_task", None)
        if task:
            task.cancel()

    # ---- public OpenAI-compatible surface ------------------------------------------------------
    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/metrics")
    async def metrics_endpoint() -> Response:
        await asyncio.to_thread(coord.queue.refresh_gauges)
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.post("/v1/files")
    async def upload_file(file: UploadFile, purpose: str = Form("batch")) -> FileObjectOut:
        data = await file.read()
        return await asyncio.to_thread(
            coord.save_file, file.filename or "input.jsonl", purpose, data
        )

    @app.get("/v1/files/{file_id}/content")
    async def get_file_content(file_id: str) -> Response:
        try:
            data = await asyncio.to_thread(coord.file_content, file_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="file not found") from None
        return Response(data, media_type="application/jsonl")

    @app.post("/v1/batches")
    async def create_batch(req: CreateBatchIn) -> BatchOut:
        try:
            return await asyncio.to_thread(coord.create_batch, req)
        except KeyError:
            raise HTTPException(status_code=404, detail="input_file_id not found") from None
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None

    @app.get("/v1/batches/{batch_id}")
    async def get_batch(batch_id: str) -> BatchOut:
        try:
            return await asyncio.to_thread(coord.get_batch, batch_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="batch not found") from None

    @app.get("/v1/batches")
    async def list_batches(limit: int = Query(20, le=100)) -> dict:
        data = await asyncio.to_thread(coord.list_batches, limit)
        return {"object": "list", "data": [b.model_dump() for b in data]}

    @app.post("/v1/batches/{batch_id}/cancel")
    async def cancel_batch(batch_id: str) -> BatchOut:
        try:
            return await asyncio.to_thread(coord.cancel_batch, batch_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="batch not found") from None

    # ---- internal worker protocol --------------------------------------------------------------
    @app.post("/internal/lease")
    async def lease(req: LeaseRequest) -> LeaseResponse:
        return await asyncio.to_thread(coord.queue.claim, req)

    @app.post("/internal/complete")
    async def complete(req: CompleteRequest) -> CompleteResponse:
        resp, finalized = await asyncio.to_thread(coord.queue.complete, req)
        for bid in finalized:
            await asyncio.to_thread(coord.finalize_batch, bid)
        return resp

    @app.post("/internal/heartbeat")
    async def heartbeat(req: HeartbeatRequest) -> HeartbeatResponse:
        return await asyncio.to_thread(coord.queue.heartbeat, req)

    return app
