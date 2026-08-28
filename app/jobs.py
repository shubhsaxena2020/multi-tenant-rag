"""Async ingestion job store (PostgreSQL + SQLite via async SQLAlchemy).
Ingestion of large documents / URL fetches can take seconds-to-minutes (network
fetch, chunking, embedding). Per the core requirement, long-running ingestion must
expose status tracking. Jobs move through explicit states:

    pending -> running -> completed
                        -> failed

The API returns a job_id immediately; clients poll GET /{tenant}/jobs/{id}.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from .db import (
    create_job as _create_job,
    delete_job as _delete_job,
    get_job as _get_job,
    list_jobs as _list_jobs,
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