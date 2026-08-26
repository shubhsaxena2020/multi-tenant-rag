"""Async ingestion job store (sqlite-backed, production-ready swap to Postgres).

Ingestion of large documents / URL fetches can take seconds-to-minutes (network
fetch, chunking, embedding). Per the core requirement, long-running ingestion must
expose status tracking. Jobs move through explicit states:

    pending -> running -> completed
                        -> failed

The API returns a job_id immediately; clients poll GET /{tenant}/jobs/{id}.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime

from .config import get_settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id       TEXT PRIMARY KEY,
    tenant_id    TEXT NOT NULL,
    kind         TEXT NOT NULL,
    status       TEXT NOT NULL,
    progress     REAL NOT NULL DEFAULT 0,
    total_chunks INTEGER DEFAULT 0,
    done_chunks  INTEGER DEFAULT 0,
    error        TEXT,
    result_doc_id TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    title        TEXT
)
"""

_DB_LOCK = threading.RLock()  # reentrant: execute schema while held is fine


def _connect(db_url: str) -> sqlite3.Connection:
    if db_url.startswith("sqlite:///"):
        import os

        path = db_url[len("sqlite:///"):]
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False)
    else:
        raise RuntimeError(f"Unsupported db_url for v1: {db_url}")
    conn.row_factory = sqlite3.Row
    return conn


def _conn() -> sqlite3.Connection:
    """Open ONE connection and ensure the schema exists. Use it for execute AND
    commit in the same call — each connect() returns a distinct connection.
    CREATE TABLE IF NOT EXISTS is idempotent, so we run it on every open (cheap and
    correct even if the db file was deleted and recreated between calls)."""
    s = get_settings()
    conn = _connect(s.db_url)
    conn.execute(_SCHEMA)
    return conn


def _now() -> str:
    return datetime.now(UTC).isoformat()


def create_job(tenant_id: str, kind: str, title: str) -> str:
    import uuid

    job_id = f"job_{uuid.uuid4().hex[:16]}"
    now = _now()
    with _DB_LOCK:
        conn = _conn()
        conn.execute(
            "INSERT INTO jobs (job_id, tenant_id, kind, status, progress, created_at, updated_at, title) "
            "VALUES (?, ?, ?, 'pending', 0, ?, ?, ?)",
            (job_id, tenant_id, kind, now, now, title),
        )
        conn.commit()
    return job_id


def update_job(
    job_id: str,
    *,
    status: str | None = None,
    progress: float | None = None,
    done_chunks: int | None = None,
    total_chunks: int | None = None,
    error: str | None = None,
    result_doc_id: str | None = None,
) -> None:
    sets, vals = [], []
    if status is not None:
        sets.append("status=?"); vals.append(status)
    if progress is not None:
        sets.append("progress=?")
        vals.append(progress)
    if done_chunks is not None:
        sets.append("done_chunks=?")
        vals.append(done_chunks)
    if total_chunks is not None:
        sets.append("total_chunks=?")
        vals.append(total_chunks)
    if error is not None:
        sets.append("error=?")
        vals.append(error)
    if result_doc_id is not None:
        sets.append("result_doc_id=?")
        vals.append(result_doc_id)
    if not sets:
        return
    sets.append("updated_at=?")
    vals.append(_now())
    vals.append(job_id)
    with _DB_LOCK:
        conn = _conn()
        conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE job_id=?", vals)
        conn.commit()


def get_job(job_id: str, tenant_id: str) -> dict | None:
    with _DB_LOCK:
        conn = _conn()
        row = conn.execute(
            "SELECT * FROM jobs WHERE job_id=? AND tenant_id=?", (job_id, tenant_id)
        ).fetchone()
    return dict(row) if row else None


def list_jobs(tenant_id: str, limit: int = 50) -> list[dict]:
    with _DB_LOCK:
        conn = _conn()
        rows = conn.execute(
            "SELECT * FROM jobs WHERE tenant_id=? ORDER BY created_at DESC LIMIT ?",
            (tenant_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_job(job_id: str, tenant_id: str) -> bool:
    with _DB_LOCK:
        conn = _conn()
        cur = conn.execute(
            "DELETE FROM jobs WHERE job_id=? AND tenant_id=?", (job_id, tenant_id)
        )
        conn.commit()
    return cur.rowcount > 0
