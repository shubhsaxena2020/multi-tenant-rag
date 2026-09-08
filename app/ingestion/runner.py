"""Background job runner for async ingestion.

A single ThreadPoolExecutor processes queued jobs. Each job fetches/embeds/upserts
and reports progress back to the jobs store. Failures are captured as `failed` with
the error message rather than crashing the worker. Metrics + logs are emitted here.

Progress push support (Task 4):
- `on_progress` callback now accepts an optional `push_func` kwarg
- When provided, progress pushes are delivered asynchronously after the DB update
- The push mechanism is handled in the async caller, not inside the sync callback
"""

from __future__ import annotations

import asyncio

from .. import jobs
from ..job_queue import get_job_queue
from ..observability import INGEST_CHUNKS, INGEST_JOBS, get_logger
from . import ingest_text, ingest_url

log = get_logger("rag")


def _run(job_id: str, tenant_id: str, kind: str, payload: dict, metadata: dict | None):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    result = None
    try:
        result = loop.run_until_complete(_run_async(job_id, tenant_id, kind, payload, metadata))
    except Exception as e:
        log.exception("ingest_job_failed")
        # Sanitize error before persisting/logging (G): keep type + short message,
        # never the full traceback/raw exception text (could leak internal detail).
        safe_err = f"{type(e).__name__}: {str(e)[:200]}"
        loop.run_until_complete(jobs.update_job(job_id, status="failed", error=safe_err))
        INGEST_JOBS.labels(status="failed").inc()
        log.error("ingest_job_failed", extra={"tenant_id": tenant_id, "job_id": job_id, "error": safe_err})
        result = {"status": "failed", "error": safe_err}
    finally:
        if result is not None:
            if result.get("status") == "completed":
                _fire_ingest_webhook(tenant_id, job_id, result, loop)
                loop.run_until_complete(jobs.update_job(
                    job_id, status="completed", progress=1.0, result_doc_id=result["result_doc_id"]
                ))
                INGEST_CHUNKS.inc(result["chunk_count"])
                INGEST_JOBS.labels(status="completed").inc()
                log.info(
                    "ingest_job_completed",
                    extra={"tenant_id": tenant_id, "job_id": job_id, "chunk_count": result["chunk_count"]},
                )
            else:
                _fire_ingest_webhook(tenant_id, job_id, result, loop)
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
            await cb["push_func"]({"job_id": cb["job_id"], "progress": cb["progress"],
                                   "done_chunks": cb["done_chunks"], "total_chunks": cb["total_chunks"]})
        except Exception as e:
            log.warning("progress_push_failed", extra={"job_id": cb["job_id"], "error": str(e)[:200]})

    return {"status": "completed", "result_doc_id": result["doc_id"], "chunk_count": result["chunk_count"]}


def _fire_ingest_webhook(tenant_id: str, job_id: str, result: dict, loop: asyncio.AbstractEventLoop) -> None:
    """Best-effort tenant ingestion webhook. Never raises; never affects the job."""
    try:
        from ..db import get_ingest_webhook_url
        from ..webhook import dispatch_webhook

        url = loop.run_until_complete(get_ingest_webhook_url(tenant_id))
        if not url:
            return
        payload = {
            "event": "ingest.job",
            "job_id": job_id,
            "tenant_id": tenant_id,
            "status": result.get("status"),
            "result_doc_id": result.get("result_doc_id"),
            "chunk_count": result.get("chunk_count"),
            "error": result.get("error"),
        }
        loop.run_until_complete(dispatch_webhook(url, payload, tenant_id))
    except Exception as e:  # noqa: BLE001 - webhook must never affect the job outcome
        log.warning("ingest_webhook_failed", extra={"job_id": job_id, "error_type": type(e).__name__})


def submit(job_id: str, tenant_id: str, kind: str, payload: dict, metadata: dict | None = None) -> None:
    get_job_queue(runner_submit=_run).enqueue(job_id, tenant_id, kind, payload, metadata)
