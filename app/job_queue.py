"""Ingestion job queue abstraction (v10.9 - horizontal worker scaling).

The durable job store lives in app/jobs.py (DB-backed). This module is the
distribution layer: it decides WHERE a pending job is executed.

Two backends:

  * inline (default) - schedule on the local worker thread. Single replica,
    no external dependencies. This is the historical behavior, preserved exactly.
  * redis - LPUSH a job reference onto a Redis list; any replica running the worker
    loop (BLPOP) claims it and runs it. Makes the worker fleet horizontally scalable
    with at-least-once delivery.
"""
from __future__ import annotations

import json
import threading
from abc import ABC, abstractmethod
from typing import Any

from .config import get_settings
from .observability import get_logger

log = get_logger("rag")

JobRef = dict[str, Any]
_JOB_QUEUE_KEY = "rag:ingest:jobs"


class JobQueue(ABC):
    """Distribution backend for pending ingestion jobs."""

    @abstractmethod
    def enqueue(self, job_id: str, tenant_id: str, kind: str, payload: dict, metadata: dict | None) -> None:
        """Enqueue a job for execution. Must be safe to call more than once for the
        same job_id (idempotent)."""
        ...

    @abstractmethod
    def dequeue(self, timeout: float = 1.0) -> JobRef | None:
        """Blocking pop of the next job ref, or None on timeout (for worker loops)."""
        ...

    @abstractmethod
    def pending_count(self) -> int:
        """Approximate number of queued-but-not-yet-claimed jobs (for metrics/health)."""
        ...


class InProcessJobQueue(JobQueue):
    """Default backend: run on a dedicated worker thread. No external services."""

    def __init__(self, runner_submit, max_workers: int = 4) -> None:
        self._submit = runner_submit
        self._sem = threading.Semaphore(max_workers)
        self._seen: set[str] = set()
        self._lock = threading.Lock()

    def enqueue(self, job_id: str, tenant_id: str, kind: str, payload: dict, metadata: dict | None) -> None:
        with self._lock:
            if job_id in self._seen:
                return
            self._seen.add(job_id)

        def _worker() -> None:
            try:
                self._submit(job_id, tenant_id, kind, payload, metadata)
            finally:
                self._sem.release()

        self._sem.acquire()
        t = threading.Thread(target=_worker, name=f"ingest-{job_id}", daemon=True)
        t.start()

    def dequeue(self, timeout: float = 1.0) -> JobRef | None:
        return None

    def pending_count(self) -> int:
        with self._lock:
            return len(self._seen)


class RedisJobQueue(JobQueue):
    """Redis-backed backend: LPUSH on enqueue, BLPOP on claim (worker side)."""

    def __init__(self, redis_url: str, key: str, runner_submit) -> None:
        import redis  # imported lazily; redis is an optional dependency

        self._r = redis.Redis.from_url(redis_url, socket_timeout=5, socket_connect_timeout=5)
        self._key = key
        self._submit = runner_submit
        try:
            self._r.ping()
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"JOB_QUEUE=redis but Redis is unreachable: {e}") from e

    def enqueue(self, job_id: str, tenant_id: str, kind: str, payload: dict, metadata: dict | None) -> None:
        added = self._r.sadd(f"{self._key}:inflight", job_id)
        if not added:
            return
        ref = json.dumps(
            {"job_id": job_id, "tenant_id": tenant_id, "kind": kind,
             "payload": payload, "metadata": metadata}
        )
        self._r.lpush(self._key, ref)

    def dequeue(self, timeout: float = 1.0) -> JobRef | None:
        item = self._r.brpop(self._key, timeout=int(timeout))
        if not item:
            return None
        _, raw = item
        ref = json.loads(raw)
        self._r.srem(f"{self._key}:inflight", ref["job_id"])
        return ref

    def pending_count(self) -> int:
        return self._r.llen(self._key)


_queue: JobQueue | None = None
_qlock = threading.Lock()


def get_job_queue(runner_submit=None) -> JobQueue:
    """Return the configured queue backend (singleton)."""
    global _queue
    if _queue is not None:
        return _queue
    with _qlock:
        if _queue is not None:
            return _queue
        s = get_settings()
        from .ingestion import runner as _runner

        backend = (s.job_queue_backend or "inline").strip().lower()
        if backend == "redis":
            connection = (s.job_queue_connection or s.redis_url or "").strip()
            if connection:
                _queue = RedisJobQueue(connection, _JOB_QUEUE_KEY, runner_submit or _runner._run)
            else:
                log.warning("job_queue_missing_connection", extra={"backend": backend, "fallback": "inline"})
                _queue = InProcessJobQueue(runner_submit or _runner._run)
        else:
            if backend not in ("inline", "inprocess", ""):
                log.warning(
                    "unknown_job_queue_backend",
                    extra={"backend": s.job_queue_backend, "fallback": "inline"},
                )
            _queue = InProcessJobQueue(runner_submit or _runner._run)
        return _queue


def reset_job_queue() -> None:
    """Test/teardown helper: drop the cached singleton so env changes take effect."""
    global _queue
    with _qlock:
        _queue = None
