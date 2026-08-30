"""Queue backend abstraction for multi-replica safety.

Provides a pluggable queue backend system enabling horizontal scaling
across multiple rag-service replicas. Single-replica deployments use
InlineBackend (jobs run inline); multi-replica deployments can substitute
Redis, SQS, or other backends via the register_backend()/get_backend() API.
"""
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from typing import Any

from app.config import get_settings
from app.observability import get_logger

logger = get_logger(__name__)


class QueueBackend(ABC):
    """Abstract base class for queue backends.

    Defines the interface that all queue backends must implement for
    job enqueue, status tracking, and lifecycle management across
    multiple service replicas.
    """

    @abstractmethod
    def submit(self, job_type: str, payload: dict[str, Any], **kwargs: Any) -> str:
        """Submit a job to the queue.

        Args:
            job_type: Type/category of job being submitted.
            payload: Job data/parameters.
            **kwargs: Additional backend-specific options.

        Returns:
            Job ID for tracking the submitted job.
        """
        raise NotImplementedError()

    @abstractmethod
    def get_next(self, timeout: float = 30.0) -> dict[str, Any] | None:
        """Get the next job to process.

        Polls the queue for the next pending job. Returns None if no
        jobs are available (or timeout expires).

        Args:
            timeout: Maximum seconds to wait for a job to appear.

        Returns:
            Job dict with keys: job_id, job_type, payload, enqueued_at,
            or None if no job available.
        """
        raise NotImplementedError()

    @abstractmethod
    def mark_done(self, job_id: str, result: dict[str, Any] | None = None) -> None:
        """Mark a job as completed.

        Args:
            job_id: The job ID returned by submit() or get_next().
            result: Optional job result data.
        """
        raise NotImplementedError()

    @abstractmethod
    def list_pending(self) -> list[dict[str, Any]]:
        """List all pending jobs in the queue.

        Returns:
            List of pending job summaries.
        """
        raise NotImplementedError()

    @abstractmethod
    def recover_orphans(self, older_than_seconds: float = 300.0) -> list[dict[str, Any]]:
        """Recover orphaned jobs from crashed/restarted workers.

        Jobs that were being processed when a worker crashed or restarted
        should be recovered and made available for re-processing.

        Args:
            older_than_seconds: Only recover jobs orphaned beyond this
                many seconds (default 5 minutes).

        Returns:
            List of recovered orphaned job summaries.
        """
        raise NotImplementedError()


class InlineBackend(QueueBackend):
    """Default queue backend for single-replica deployments.

    Runs jobs inline (synchronously) within the calling process.
    Suitable for development, testing, and production deployments that
    run a single service instance. Enables the register_backend()/get_backend()
    API to be used uniformly whether or not an external queue is configured.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._completed: dict[str, dict[str, Any]] = {}

    def submit(self, job_type: str, payload: dict[str, Any], **kwargs: Any) -> str:
        job_id = f"inline-{int(time.time() * 1000)}-{id(self)}"
        job = {
            "job_id": job_id,
            "job_type": job_type,
            "payload": payload,
            "enqueued_at": time.time(),
            "status": "submitted",
        }
        self._jobs[job_id] = job
        logger.info("job_submitted", extra={"job_id": job_id, "job_type": job_type})
        return job_id

    def get_next(self, timeout: float = 30.0) -> dict[str, Any] | None:
        # Inline backend processes jobs immediately; nothing to poll.
        return None

    def mark_done(self, job_id: str, result: dict[str, Any] | None = None) -> None:
        if job_id in self._jobs:
            self._jobs[job_id]["status"] = "completed"
            if result is not None:
                self._jobs[job_id]["result"] = result
            self._completed[job_id] = self._jobs.pop(job_id)
            logger.info("job_completed", extra={"job_id": job_id})
        else:
            logger.warning("job_not_found", extra={"job_id": job_id})

    def list_pending(self) -> list[dict[str, Any]]:
        return [
            j for j in self._jobs.values() if j["status"] != "completed"
        ]

    def recover_orphans(self, older_than_seconds: float = 300.0) -> list[dict[str, Any]]:
        # Inline backend has no orphans — jobs either complete or stay submitted.
        return []


# ---------------------------------------------------------------------------\
# Backend registry: select and retrieve the active backend instance.\
# ---------------------------------------------------------------------------\n

_backend: QueueBackend | None = None

# Auto-initialize from config on module import
_settings = get_settings()
if _settings.job_queue_backend == "inline":
    _backend = InlineBackend()
    logger.info("queue_backend_auto_registered", extra={"backend": "InlineBackend"})
else:
    # Default to InlineBackend until concrete external backends are implemented
    _backend = InlineBackend()
    logger.info("queue_backend_default_fallback", extra={"backend": "InlineBackend"})


def register_backend(backend: QueueBackend | type[QueueBackend] | str = "inline") -> None:
    """Register the active queue backend for multi-replica safety.

    Accepts an instantiated backend, a backend class (will be instantiated),
    or a string alias ("inline", "redis", "sqs", etc.). When "inline" is
    selected, InlineBackend is used — enabling uniform submit/get_next/mark_done/
    list_pending/recover_orphans API regardless of deployment scale.

    Args:
        backend: Backend instance, class, or string identifier.

    Raises:
        ValueError: If the backend string is not recognised.
    """
    global _backend

    if isinstance(backend, str):
        name = backend.strip().lower()
        if name == "inline":
            _backend = InlineBackend()
        elif name in ("redis", "sqs", "cloudtasks", "rq"):
            # Placeholder: concrete implementations would be added here.
            raise NotImplementedError(
                f"Backend '{name}' is not yet implemented. "
                "Use 'inline' or provide a custom QueueBackend subclass."
            )
        else:
            raise ValueError(
                f"Unknown backend '{name}'. Choose from: inline, redis, sqs, cloudtasks, rq"
            )
    elif isinstance(backend, type) and issubclass(backend, QueueBackend):
        _backend = backend()
    elif isinstance(backend, QueueBackend):
        _backend = backend
    else:
        # Default to InlineBackend
        _backend = InlineBackend()

    logger.info("queue_backend_registered", extra={"backend": type(_backend).__name__})


def get_backend() -> QueueBackend:
    """Retrieve the currently registered queue backend instance.

    Returns:
        The active QueueBackend subclass instance.

    Raises:
        RuntimeError: If no backend has been registered (should not happen
            as InlineBackend is the default).
    """
    if _backend is None:
        register_backend("inline")
    assert _backend is not None, "Queue backend must be registered before use"
    return _backend


def submit(job_type: str, payload: dict[str, Any], **kwargs: Any) -> str:
    """Submit a job to the active queue backend.

    Backend-agnostic API. The underlying backend (InlineBackend or
    CloudTasksBackend or other) handles job enqueue.

    Args:
        job_type: Type/category of job being submitted.
        payload: Job data/parameters.
        **kwargs: Additional backend-specific options.

    Returns:
        Job ID for tracking the submitted job.
    """
    backend = get_backend()
    return backend.submit(job_type, payload, **kwargs)


def get_next(timeout: float = 30.0) -> dict[str, Any] | None:
    """Get the next job to process from the active queue backend.

    Polls the active backend for the next pending job. Returns None if
    no jobs are available.

    Args:
        timeout: Maximum seconds to wait for a job to appear.

    Returns:
        Job dict with keys: job_id, job_type, payload, enqueued_at,
        or None if no job available.
    """
    backend = get_backend()
    return backend.get_next(timeout)


def mark_done(job_id: str, result: dict[str, Any] | None = None) -> None:
    """Mark a job as completed in the active queue backend.

    Args:
        job_id: The job ID returned by submit() or get_next().
        result: Optional job result data.
    """
    backend = get_backend()
    backend.mark_done(job_id, result)


def list_pending() -> list[dict[str, Any]]:
    """List all pending jobs in the active queue backend.

    Returns:
        List of pending job summaries.
    """
    backend = get_backend()
    return backend.list_pending()


def recover_orphans(older_than_seconds: float = 300.0) -> list[dict[str, Any]]:
    """Recover orphaned jobs from crashed/restarted workers.

    Jobs that were being processed when a worker crashed or restarted
    should be recovered and made available for re-processing.

    Args:
        older_than_seconds: Only recover jobs orphaned beyond this
            many seconds (default 5 minutes).

    Returns:
        List of recovered orphaned job summaries.
    """
    backend = get_backend()
    return backend.recover_orphans(older_than_seconds)