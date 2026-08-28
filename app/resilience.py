"""Resilience primitives for the RAG service.

Production retrieval paths had NO error handling: a Qdrant/embed/rerank failure
surfaced as a raw HTTP 500 stack trace to every chatbot query. These primitives
make the service degrade gracefully (v9-1):

- `RagError`: typed exception carrying a safe `public_detail` (never leaks internals)
  and a `degraded` flag, used by the global handler in main.py.
- `with_retry`: exponential backoff with jitter for transient backend failures
  (Qdrant timeouts, embedder OOM, reranker hiccups).
- `with_timeout`: bounded wall-clock guard around a coroutine/blocking call so a
  sick backend hangs the request instead of the whole replica.
- `CircuitBreaker`: per-dependency (qdrant/embed/rerank) failure counter that
  trips OPEN after N consecutive errors and fails fast for the cooldown window,
  then half-opens to probe recovery. Keeps one bad Qdrant from hanging every call.

No external deps — pure stdlib + asyncio.
"""
from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from .config import get_settings

T = TypeVar("T")

# Exceptions we treat as transient (worth retrying / tripping the breaker on).
_TRANSIENT = ()


class RagError(Exception):
    """Typed service error. `public_detail` is safe to return to clients; `internal`
    is logged only (never leaked). `degraded=True` means the service is partially
    available and the caller should expect a `degraded` flag in the response."""

    def __init__(self, public_detail: str, *, internal: str | None = None, degraded: bool = True):
        super().__init__(public_detail)
        self.public_detail = public_detail
        self.internal = internal
        self.degraded = degraded


class CircuitOpen(RagError):
    """Raised when a dependency's circuit breaker is OPEN (fast-fail)."""

    def __init__(self, dependency: str):
        super().__init__(
            f"{dependency} temporarily unavailable (degraded mode)",
            internal=f"circuit breaker OPEN for {dependency}",
            degraded=True,
        )
        self.dependency = dependency


@dataclass
class _BreakerState:
    failures: int = 0
    opened_at: float = 0.0
    half_open_probe: bool = False


_BREAKERS: dict[str, _BreakerState] = {}


def _breaker_state(dependency: str) -> _BreakerState:
    st = _BREAKERS.get(dependency)
    if st is None:
        st = _BreakerState()
        _BREAKERS[dependency] = st
    return st


def circuit_status(dependency: str) -> str:
    """OPEN | HALF_OPEN | CLOSED — for health/metrics."""
    s = get_settings()
    st = _BREAKERS.get(dependency)
    if st is None:
        return "closed"
    if st.failures >= s.cb_failure_threshold and (time.monotonic() - st.opened_at) < s.cb_cooldown_s:
        return "open" if not st.half_open_probe else "half_open"
    return "closed"


def _breaker_open(dependency: str) -> bool:
    """Return True if the breaker is currently OPEN (caller should fast-fail)."""
    s = get_settings()
    st = _breaker_state(dependency)
    if st.failures < s.cb_failure_threshold:
        return False
    if (time.monotonic() - st.opened_at) >= s.cb_cooldown_s:
        # cooldown elapsed -> allow a single half-open probe
        st.half_open_probe = True
        return False
    return True


def _breaker_record_failure(dependency: str) -> None:
    s = get_settings()
    st = _breaker_state(dependency)
    if not st.half_open_probe:
        st.failures += 1
        if st.failures >= s.cb_failure_threshold:
            st.opened_at = time.monotonic()
    else:
        # probe failed -> stay open, reset cooldown
        st.opened_at = time.monotonic()
        st.half_open_probe = False


def _breaker_record_success(dependency: str) -> None:
    st = _BREAKERS.get(dependency)
    if st is not None:
        st.failures = 0
        st.opened_at = 0.0
        st.half_open_probe = False


def with_retry(
    dependency: str,
    fn: Callable[[], T],
    *,
    max_attempts: int | None = None,
    base_delay: float | None = None,
    max_delay: float | None = None,
) -> T:
    """Run a (possibly blocking) backend call with exponential backoff + jitter.

    On transient failure, retries up to `max_attempts`; records failures into the
    named dependency's circuit breaker. Raises RagError on exhaustion (degraded).
    """
    s = get_settings()
    max_attempts = max_attempts or s.retry_max_attempts
    base_delay = base_delay if base_delay is not None else s.retry_base_delay_s
    max_delay = max_delay if max_delay is not None else s.retry_max_delay_s

    if _breaker_open(dependency):
        raise CircuitOpen(dependency)

    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            result = fn()
            _breaker_record_success(dependency)
            return result
        except CircuitOpen:
            raise
        except Exception as exc:
            last_exc = exc
            if attempt >= max_attempts:
                break
            delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
            delay = delay * (0.5 + random.random())  # full jitter
            time.sleep(delay)

    _breaker_record_failure(dependency)
    if isinstance(last_exc, RagError):
        raise
    raise RagError(
        f"{dependency} unavailable after {max_attempts} attempts (degraded mode)",
        internal=f"{type(last_exc).__name__}: {last_exc}",
        degraded=True,
    )


async def with_retry_async(
    dependency: str,
    coro_fn: Callable[[], Awaitable[T]],
    *,
    max_attempts: int | None = None,
    base_delay: float | None = None,
    max_delay: float | None = None,
) -> T:
    """Async variant of with_retry for coroutine backend calls."""
    s = get_settings()
    max_attempts = max_attempts or s.retry_max_attempts
    base_delay = base_delay if base_delay is not None else s.retry_base_delay_s
    max_delay = max_delay if max_delay is not None else s.retry_max_delay_s

    if _breaker_open(dependency):
        raise CircuitOpen(dependency)

    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            result = await coro_fn()
            _breaker_record_success(dependency)
            return result
        except CircuitOpen:
            raise
        except Exception as exc:
            last_exc = exc
            if attempt >= max_attempts:
                break
            delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
            delay = delay * (0.5 + random.random())
            await asyncio.sleep(delay)

    _breaker_record_failure(dependency)
    if isinstance(last_exc, RagError):
        raise
    raise RagError(
        f"{dependency} unavailable after {max_attempts} attempts (degraded mode)",
        internal=f"{type(last_exc).__name__}: {last_exc}",
        degraded=True,
    )


async def with_timeout(coro: Awaitable[T], seconds: float, dependency: str = "backend") -> T:
    """Bound a coroutine with a timeout; on expiry raise RagError (degraded)."""
    try:
        return await asyncio.wait_for(asyncio.shield(coro), timeout=seconds)
    except TimeoutError:
        raise RagError(
            f"{dependency} timed out after {seconds:.0f}s (degraded mode)",
            internal=f"TimeoutError after {seconds}s on {dependency}",
            degraded=True,
        )


def reset_breakers() -> None:
    """Clear all breaker state (tests / config reload)."""
    _BREAKERS.clear()
