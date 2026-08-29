"""Ingestion job queue abstraction (v10.9 — horizontal worker scaling).

The durable job *store* lives in app/jobs.py (DB-backed). This module is the
*distribution* layer: it decides WHERE a pending job is executed.

Two backends:

  * inprocess (default) — schedule on the local ThreadPoolExecutor. Single replica,
    no external dependencies. This is the historical behavior, preserved exactly.
  * redis — LPUSH a job reference onto JOB_QUEUE_KEY; any replica running the worker
    loop (BLPOP) claims it and runs it. Makes the worker fleet horizontally scalable
    with at-least-once delivery (jobs are re-enqueued by requeue_orphaned_jobs on boot
    if a replica dies mid-flight).

Design properties:
  * Single chokepoint: runner.submit() -> get_job_queue().enqueue(). Swapping the
    backend never touches call sites.
  * Idempotent enqueue: keyed by job_id. A job already pending/running is NOT
    re-enqueued, so duplicate triggers can't double-process.
  * At-least-once: a job reference is durable in Redis; if a worker dies, the orphan
    recovery (db.requeue_orphaned_jobs) re-arms pending jobs on next boot.
"""
from __future__ import annotations

import json
import threading
from abc import ABC, abstractmethod
from typing import Any

from .config import get_settings
from .observability import get_logger

log = get_logger("rag")

# A "job ref" is the minimal payload needed to re-invoke the runner.
# We enqueue only the reference; the worker re-reads full state from the DB store.
JobRef = dict[str, Any]


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
    """Default backend: run on a dedicated worker thread. No external services.

    Each job gets a FRESH daemon thread so its asyncio loop is never contaminated by a
    stale "running loop" left on a reused ThreadPoolExecutor thread (a real footgun when
    the executor is process-global and shared across many call sites). A semaphore bounds
    concurrency to `max_workers`, preserving the old 4-worker cap.
    """

    def __init__(self, runner_submit, max_workers: int = 4) -> None:
        # runner_submit is runner._run (the actual job fn) — injected to avoid a circular
        # import at module load time.
        self._submit = runner_submit
        self._sem = threading.Semaphore(max_workers)
        self._seen: set[str] = set()
        self._lock = threading.Lock()

    def enqueue(self, job_id: str, tenant_id: str, kind: str, payload: dict, metadata: dict | None) -> None:
        with self._lock:
            # Idempotency: never double-schedule an in-flight job on this replica.
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
        # In-process has no pull loop; execution is push-based via enqueue().
        return None

    def pending_count(self) -> int:
        with self._lock:
            return len(self._seen)


class RedisJobQueue(JobQueue):
    """Redis-backed backend: LPUSH on enqueue, BLPOP on claim (worker side).

    Requires redis_url to be configured. If a Redis connection cannot be established,
    enqueue falls back to raising at construction time so misconfig is caught early
    rather than silently dropping jobs.
    """

    def __init__(self, redis_url: str, key: str, runner_submit) -> None:
        import redis  # imported lazily; redis is an optional dependency

        self._r = redis.Redis.from_url(redis_url, socket_timeout=5, socket_connect_timeout=5)
        self._key = key
        self._submit = runner_submit
        # Validate connectivity up front (fail-fast on misconfig).
        try:
            self._r.ping()
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"JOB_QUEUE=redis but Redis is unreachable: {e}") from e

    def enqueue(self, job_id: str, tenant_id: str, kind: str, payload: dict, metadata: dict | None) -> None:
        # Idempotent: only push if not already queued. We track in-flight ids in a Redis
        # SET so duplicates across replicas are dropped (at-least-once, no double-run).
        # sadd() returns the number of NEW members added — 0 means already in-flight.
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
        # Remove from inflight once claimed (the DB `running` state is the real guard;
        # this just prevents re-enqueue storms if the orphan recovery re-arms it).
        self._r.srem(f"{self._key}:inflight", ref["job_id"])
        return ref

    def pending_count(self) -> int:
        return self._r.llen(self._key)


_queue: JobQueue | None = None
_qlock = threading.Lock()


def get_job_queue(runner_submit=None) -> JobQueue:
    """Return the configured queue backend (singleton).

    `runner_submit` is required only on first construction (to inject the executor fn).
    """
    global _queue
    if _queue is not None:
        return _queue
    with _qlock:
        if _queue is not None:
            return _queue
        s = get_settings()
        if s.job_queue_backend == "redis" and s.redis_url:
            from .ingestion import runner as _runner

            _queue = RedisJobQueue(s.redis_url, s.job_queue_key, runner_submit or _runner._run)
        else:
            if s.job_queue_backend not in ("inprocess", ""):
                log.warning(
                    "unknown_job_queue_backend",
                    extra={"backend": s.job_queue_backend, "fallback": "inprocess"},
                )
            from .ingestion import runner as _runner

            _queue = InProcessJobQueue(runner_submit or _runner._run)
        return _queue


def reset_job_queue() -> None:
    """Test/teardown helper: drop the cached singleton so env changes take effect."""
    global _queue
    with _qlock:
        _queue = None
