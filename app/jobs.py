"""Async ingestion job store (PostgreSQL + SQLite via async SQLAlchemy).

Ingestion of large documents / URL fetches can take seconds-to-minutes (network
fetch, chunking, embedding). Per the core requirement, long-running ingestion must
expose status tracking. Jobs move through explicit states:

    pending -> running -> completed
        -> failed

The API returns a job_id immediately; clients poll GET /{tenant}/jobs/{id}.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

from sqlalchemy import update as sa_update
from sqlalchemy.orm import Session

from .db import (
    create_job as _create_job,
)
from .db import (
    delete_job as _delete_job,
)
from .db import (
    get_job as _get_job,
)
from .db import (
    list_jobs as _list_jobs,
)
from .db import (
    update_job as _update_job,
)
from .db import (
    requeue_orphaned_jobs as _requeue_orphaned_jobs,
)


async def create_job(tenant_id: str, kind: str, title: str) -> str:
    return await _create_job(tenant_id, kind, title)


async def update_job(
    job_id: str,
    *,
    status: str | None = None,
    progress: float | None = None,
    done_chunks: int | None = None,
    total_chunks: int | None = None,
    error: str | None = None,
    result_doc_id: str | None = None,
) -> None:
    await _update_job(
        job_id,
        status=status,
        progress=progress,
        done_chunks=done_chunks,
        total_chunks=total_chunks,
        error=error,
        result_doc_id=result_doc_id,
    )


async def get_job(job_id: str, tenant_id: str) -> dict | None:
    return await _get_job(job_id, tenant_id)


async def list_jobs(tenant_id: str, limit: int = 50) -> list[dict]:
    return await _list_jobs(tenant_id, limit)


async def delete_job(job_id: str, tenant_id: str) -> bool:
    return await _delete_job(job_id, tenant_id)


def enqueue_job(tenant_id: str, kind: str, title: str | None = None, payload: dict | None = None) -> str:
    """Synchronously create a job for legacy callers.

    A separate thread is used when called from an active event loop because
    ``run_until_complete`` cannot be nested in that loop.
    """
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    async def _create() -> str:
        return await _create_job(tenant_id, kind, title or kind)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_create())

    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, _create()).result()


async def get_job_status(job_id: str, tenant_id: str) -> dict | None:
    """Get the status of a queued ingestion job."""
    return await _get_job(job_id, tenant_id)


async def requeue_orphaned_jobs(session: object | None = None) -> int:
    """v9-5: durable job orchestration / crash recovery.

    A job left in `running` when a worker died (OOM, deploy, crash) would otherwise be
    stuck forever. On startup we reset orphaned `running` jobs back to `pending` so a
    worker can pick them up. Additionally, failed jobs are reset to `pending` so they can
    be retried. Returns the number of recovered jobs.

    Multi-replica safety: uses a time-bounded recovery token stored in the DB so that
    only one replica across N workers performs recovery at a time. Other replicas skip
    recovery if a recent token already exists, preventing duplicate recovery passes.

    For horizontal scale with N workers, front this with an at-least-once queue (Cloud Tasks
    / RQ / Celery) that calls the existing job runner; this recovery handles the single-replica
    VPS case where the in-memory queue is lost on restart.

    Idempotent: calling multiple times with the same token returns the same count;
    calling without a token after a recent recovery is a no-op.
    """
    return await _requeue_orphaned_jobs(session)
