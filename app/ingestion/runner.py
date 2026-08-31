"""Background job runner for async ingestion.

A single ThreadPoolExecutor processes queued jobs. Each job fetches/embeds/upserts
and reports progress back to the jobs store. Failures are captured as `failed` with
the error message rather than crashing the worker. Metrics + logs are emitted here.

Progress push support (Task 4):
- `on_progress` callback now accepts an optional `push_func` kwarg
- When provided, progress pushes are delivered asynchronously after the DB update
- The push mechanism is handled in the async caller, not inside the sync callback

Job queue backend (Phase J, Task 48):
- "inline" (default): uses ThreadPoolExecutor, single-replica only
- "redis": uses RQ-style wrapper with Redis for multi-replica safety
- "rq" / "celery": external queue backend integration points
"""

from __future__ import annotations

import asyncio
import os
import threading
from concurrent.futures import ThreadPoolExecutor

from .. import jobs
from ..config import get_settings
from ..observability import INGEST_CHUNKS, INGEST_JOBS, get_logger
from . import ingest_text, ingest_url

log = get_logger("rag")

_settings = get_settings()

_executor: ThreadPoolExecutor | None = None
_lock = threading.Lock()

_job_queue_backend: str = _settings.job_queue_backend
_job_queue_connection: str = _settings.job_queue_connection


def _ensure_executor() -> ThreadPoolExecutor:
    global _executor
    with _lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ingest")
    return _executor


def _is_redis_backend() -> bool:
    """Check if we should use Redis-backed job queue for multi-replica safety."""
    return bool(_job_queue_backend and _job_queue_backend != "inline" and _job_queue_backend.startswith("redis"))


def submit(job_id: str, tenant_id: str, kind: str, payload: dict, metadata: dict | None = None) -> None:
    """Submit an ingestion job using the configured queue backend.

    - "inline" (default): uses ThreadPoolExecutor, single-replica only
    - "redis": uses RQ-style wrapper with Redis for multi-replica safety
    - "rq" / "celery": external queue backend integration points
    """
    if _is_redis_backend() and _job_queue_connection:
        _submit_redis(job_id, tenant_id, kind, payload, metadata)
    else:
        _executor = _ensure_executor()
        _executor.submit(_run, job_id, tenant_id, kind, payload, metadata)


def _submit_redis(job_id: str, tenant_id: str, kind: str, payload: dict, metadata: dict | None) -> None:
    """Submit job to Redis-backed queue for multi-replica safety.

    Uses RQ-style queue with Redis connection. When Redis URL is configured,
    jobs are enqueued to a shared queue visible across all replicas.
    """
    try:
        import rq
        from rq import Queue

        redis_url = _job_queue_connection or os.getenv("REDIS_URL", "")
        if not redis_url:
            log.warning("redis_queue_configured_but_no_url_falling_back_to_inline")
            _executor = _ensure_executor()
            _executor.submit(_run, job_id, tenant_id, kind, payload, metadata)
            return

        queue = Queue("ingest", connection=redis_url)
        queue.enqueue(_run, job_id, tenant_id, kind, payload, metadata)
        INGEST_JOBS.labels(backend="redis").inc()
        log.info("ingest_job_enqueued_to_redis", extra={"tenant_id": tenant_id, "job_id": job_id})
    except ImportError:
        log.warning("rq_not_installed_falling_back_to_inline", extra={"job_id": job_id})
        _executor = _ensure_executor()
        _executor.submit(_run, job_id, tenant_id, kind, payload, metadata)
    except Exception as e:
        log.error("redis_queue_failed_falling_back_to_inline", extra={
            "job_id": job_id, "error": str(e)[:200]
        })
        _executor = _ensure_executor()
        _executor.submit(_run, job_id, tenant_id, kind, payload, metadata)


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

    # Track progress push callbacks for later execution
    push_callbacks: list[dict] = []

    if kind == "url":
        def on_progress(done: int, total: int, push_func=None):
            # Schedule the DB update
            task = asyncio.create_task(jobs.update_job(
                job_id, progress=0.1 + 0.9 * (done / total),
                done_chunks=done, total_chunks=total
            ))
            progress_tasks.append(task)
            # Record the push request for later async execution
            if push_func:
                push_callbacks.append({
                    "job_id": job_id,
                    "progress": 0.1 + 0.9 * (done / total),
                    "done_chunks": done, "total_chunks": total,
                    "push_func": push_func,
                })

        from . import doc_key_for_url
        result = await ingest_url(
            tenant_id, payload["url"], payload.get("title"),
            metadata, on_progress=on_progress, acl=payload.get("acl"),
            doc_key=doc_key_for_url(payload["url"]),
        )
    else:
        def on_progress(done: int, total: int, push_func=None):
            # Schedule the DB update
            task = asyncio.create_task(jobs.update_job(
                job_id, progress=done / total,
                done_chunks=done, total_chunks=total
            ))
            progress_tasks.append(task)
            # Record the push request for later async execution
            if push_func:
                push_callbacks.append({
                    "job_id": job_id,
                    "progress": done / total,
                    "done_chunks": done, "total_chunks": total,
                    "push_func": push_func,
                })

        from . import doc_key_for_text
        title = payload.get("title") or "untitled"
        text = payload.get("text") or payload.get("content") or ""
        ct = payload.get("content_type", "text")
        result = await ingest_text(
            tenant_id, title, text, ct, metadata,
            on_progress=on_progress, acl=payload.get("acl"),
            doc_key=doc_key_for_text(title, ct),
        )

    # Wait for all progress DB update tasks to complete before finishing
    if progress_tasks:
        await asyncio.gather(*progress_tasks, return_exceptions=True)

    # Execute all progress push callbacks asynchronously (webhook/SSE delivery)
    for cb in push_callbacks:
        try:
            await cb["push_func"]("{\"job_id\": \"" + cb["job_id"] + "\", \"progress\": " + str(cb["progress"]) + ", \"done_chunks\": " + str(cb["done_chunks"]) + ", \"total_chunks\": " + str(cb["total_chunks"]) + "}")
        except Exception as e:
            log.warning("progress_push_failed", extra={"job_id": cb["job_id"], "error": str(e)[:200]})

    await jobs.update_job(job_id, status="completed", progress=1.0, result_doc_id=result["doc_id"])
    INGEST_CHUNKS.inc(result["chunk_count"])
    INGEST_JOBS.labels(status="completed").inc()
    log.info("ingest_job_completed", extra={"tenant_id": tenant_id, "job_id": job_id, "chunk_count": result["chunk_count"]})