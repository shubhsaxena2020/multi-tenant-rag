"""Per-tenant and per-IP rate limiting.

Token-bucket in memory. For a single service instance this is sufficient and adds no
dependency. For multi-instance horizontal scaling, swap the bucket store for Redis
(see DEPLOYMENT.md) — the interface (`RateLimiter.hit`) stays the same.

Limits (configurable via settings):
  RATE_PER_TENANT_PER_MIN  default 600   (tenant API key)
  RATE_PER_IP_PER_MIN      default 120   (client IP, defense against key sharing)
Ingestion jobs are additionally throttled (RATE_INGEST_JOBS_PER_MIN) to protect the
embedding worker pool.
"""
from __future__ import annotations

import threading
import time

from fastapi import HTTPException, Request, status

from .config import get_settings


class _Bucket:
    __slots__ = ("_cap", "_refill", "tokens", "updated")

    def __init__(self, capacity: int, refill_per_sec: float):
        self.tokens = float(capacity)
        self.updated = time.time()
        self._cap = capacity
        self._refill = refill_per_sec

    def consume(self, n: int = 1) -> bool:
        now = time.time()
        self.tokens = min(self._cap, self.tokens + (now - self.updated) * self._refill)
        self.updated = now
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False


class RateLimiter:
    def __init__(self):
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def _bucket(self, key: str, capacity: int, refill_per_sec: float) -> _Bucket:
        b = self._buckets.get(key)
        if b is None:
            b = _Bucket(capacity, refill_per_sec)
            self._buckets[key] = b
        return b

    def hit(self, key: str, capacity: int, per_min: int, cost: int = 1) -> None:
        refill = capacity / 60.0 if per_min else 0.0
        with self._lock:
            b = self._bucket(key, capacity, refill)
            if not b.consume(cost):
                retry = max(1, int((cost - b.tokens) / refill)) if refill else 60
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=f"rate limit exceeded for {key.split(':')[0]}; retry after {retry}s",
                    headers={"Retry-After": str(retry)},
                )


_limiter = RateLimiter()


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def rate_limit(request: Request, tenant_id: str | None = None) -> None:
    s = get_settings()
    # per-IP (defense against a leaked/shared key)
    _limiter.hit(f"ip:{_client_ip(request)}", s.rate_per_ip_per_min, s.rate_per_ip_per_min)
    # per-tenant (if resolved)
    if tenant_id:
        _limiter.hit(f"tenant:{tenant_id}", s.rate_per_tenant_per_min, s.rate_per_tenant_per_min)
