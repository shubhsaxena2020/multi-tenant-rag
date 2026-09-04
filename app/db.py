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


async def requeue_orphaned_jobs(session: AsyncSession | None = None) -> int:
    """v9-5: durable job orchestration / crash recovery.

    A job left in `running` when a worker died (OOM, deploy, crash) would otherwise be
    stuck forever. On startup we reset orphaned `running` jobs back to `pending` so a
    worker can pick them up. Additionally, failed jobs are reset to `pending` so they can
    be retried. Returns the number of recovered jobs.

    NOTE: the jobs table is already the durable source of truth (SQLite/Postgres). For
    horizontal scale with N workers, front this with an at-least-once queue (Cloud Tasks /
    RQ / Celery) that calls the existing job runner; this recovery handles the single-replica
    VPS case where the in-memory queue is lost on restart.
    async with (session or get_session_maker())() as s:
        now = datetime.now(UTC)
        stmt = (
            update(Job)
            .where(Job.status.in_(["running", "failed"]))
            .values(status="pending", progress=0.0, updated_at=now)
        )
        result = await s.execute(stmt)
        await s.commit()
        return result.rowcount