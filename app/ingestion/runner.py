"""Background job runner for async ingestion.

A single ThreadPoolExecutor processes queued jobs. Each job fetches/embeds/upserts
and reports progress back to the jobs store. Failures are captured as `failed` with
the error message rather than crashing the worker.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from .. import jobs
from . import ingest_text, ingest_url

_executor: ThreadPoolExecutor | None = None
_lock = threading.Lock()


def _ensure_executor() -> ThreadPoolExecutor:
    global _executor
    with _lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ingest")
    return _executor


def _run(job_id: str, tenant_id: str, kind: str, payload: dict, metadata: dict | None):
    try:
        jobs.update_job(job_id, status="running", progress=0.05)
        if kind == "url":
            # fetch happens inside ingest_url; bump progress after fetch
            def on_progress(done, total):
                jobs.update_job(job_id, progress=0.1 + 0.9 * (done / total), done_chunks=done, total_chunks=total)

            result = ingest_url(tenant_id, payload["url"], payload.get("title"), metadata, on_progress=on_progress)
        else:
            def on_progress(done, total):
                jobs.update_job(job_id, progress=done / total, done_chunks=done, total_chunks=total)

            text = payload.get("text") or payload.get("content") or ""
            result = ingest_text(tenant_id, payload.get("title", "untitled"), text, payload.get("content_type", "text"), metadata, on_progress=on_progress)
        jobs.update_job(job_id, status="completed", progress=1.0, result_doc_id=result["doc_id"])
    except Exception as e:  # noqa: BLE001
        jobs.update_job(job_id, status="failed", error=str(e)[:2000])


def submit(job_id: str, tenant_id: str, kind: str, payload: dict, metadata: dict | None = None) -> None:
    _ensure_executor().submit(_run, job_id, tenant_id, kind, payload, metadata)
