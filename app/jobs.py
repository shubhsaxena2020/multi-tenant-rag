"""Async ingestion job store (PostgreSQL + SQLite via async SQLAlchemy).
Ingestion of large documents / URL fetches can take seconds-to-minutes (network
fetch, chunking, embedding). Per the core requirement, long-running ingestion must
expose status tracking. Jobs move through explicit states:

    pending -> running -> completed
                        -> failed

The API returns a job_id immediately; clients poll GET /{tenant}/jobs/{id}.
"""
from __future__ import annotations

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
