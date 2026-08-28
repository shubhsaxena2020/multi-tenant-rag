"""Background job runner for async ingestion.

A single ThreadPoolExecutor processes queued jobs. Each job fetches/embeds/upserts
and reports progress back to the jobs store. Failures are captured as `failed` with
the error message rather than crashing the worker. Metrics + logs are emitted here.
"""
from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

from .. import jobs
from ..observability import INGEST_CHUNKS, INGEST_JOBS, get_logger
from . import ingest_text, ingest_url

log = get_logger("rag")

_executor: ThreadPoolExecutor | None = None
_lock = threading.Lock()


def _ensure_executor() -> ThreadPoolExecutor:
    global _executor
    with _lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ingest")
    return _executor


def _run(job_id: str, tenant_id: str, kind: str, payload: dict, metadata: dict | None):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_run_async(job_id, tenant_id, kind, payload, metadata))
    except Exception as e:
        log.exception("ingest_job_failed")
        # Sanitize error before persisting/logging (G): keep type + short message,
        # never the full traceback/raw exception text (could leak internal detail).
        safe_err = f"{type(e).__name__}: {str(e)[:200]}"
        loop.run_until_complete(jobs.update_job(job_id, status="failed", error=safe_err))
        INGEST_JOBS.labels(status="failed").inc()
        log.error("ingest_job_failed", extra={"tenant_id": tenant_id, "job_id": job_id, "error": safe_err})
    finally:
        loop.close()


async def _run_async(job_id: str, tenant_id: str, kind: str, payload: dict, metadata: dict | None):
    await jobs.update_job(job_id, status="running", progress=0.05)
    progress_tasks = []

    if kind == "url":
        def on_progress(done: int, total: int):
            # Schedule the update but keep track of the task
            task = asyncio.create_task(jobs.update_job(
                job_id, progress=0.1 + 0.9 * (done / total), 
                done_chunks=done, total_chunks=total
            ))
            progress_tasks.append(task)

        result = await ingest_url(
            tenant_id, payload["url"], payload.get("title"), 
            metadata, on_progress=on_progress, acl=payload.get("acl")
        )
    else:
        def on_progress(done: int, total: int):
            # Schedule the update but keep track of the task
            task = asyncio.create_task(jobs.update_job(
                job_id, progress=done / total, 
                done_chunks=done, total_chunks=total
            ))
            progress_tasks.append(task)

        text = payload.get("text") or payload.get("content") or ""
        result = await ingest_text(
            tenant_id, payload.get("title", "untitled"), text, 
            payload.get("content_type", "text"), metadata, 
            on_progress=on_progress, acl=payload.get("acl")
        )

    # Wait for all progress update tasks to complete before finishing
    if progress_tasks:
        await asyncio.gather(*progress_tasks, return_exceptions=True)

    await jobs.update_job(job_id, status="completed", progress=1.0, result_doc_id=result["doc_id"])
    INGEST_CHUNKS.inc(result["chunk_count"])
    INGEST_JOBS.labels(status="completed").inc()
    log.info("ingest_job_completed", extra={"tenant_id": tenant_id, "job_id": job_id, "chunk_count": result["chunk_count"]})


def submit(job_id: str, tenant_id: str, kind: str, payload: dict, metadata: dict | None = None) -> None:
    _ensure_executor().submit(_run, job_id, tenant_id, kind, payload, metadata)