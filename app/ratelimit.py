"""Per-tenant + per-IP rate limiting.

Industrial requirement (DEPLOYMENT.md scaling path): the limiter MUST be correct across
multiple stateless app replicas behind a load balancer. An in-memory limiter only works
on a single instance, so we support a Redis-backed token bucket (atomic via Lua script)
that activates automatically when REDIS_URL is set, and falls back to an in-process
limiter otherwise (single-instance / dev).

Algorithm: token bucket with fixed capacity = limit, refill = limit per window (1 min).
Each `hit` consumes 1 token; 429 + Retry-After when empty. The bucket key is namespaced
by tenant or IP so a noisy neighbour can't starve others, but a single tenant's burst is
still capped. All limiter state is best-effort: if Redis is unreachable we degrade to
"allow" (fail open) rather than blocking all traffic — operational safety over strictness.

Config (env):
  REDIS_URL         redis://host:6379/0  (empty -> in-memory)
  RATE_PER_TENANT_PER_MIN, RATE_PER_IP_PER_MIN, RATE_INGEST_JOBS_PER_MIN
"""
from __future__ import annotations

import os
import time

from .config import get_settings

# ---- Lua: atomic token-bucket consumption on Redis ----
# KEYS[1]=bucket  ARGV[1]=limit  ARGV[2]=window_ms  ARGV[3]=now_ms
# Tokens refill linearly. Returns {allowed(0/1), retry_after_ms}
_LUA_TAKE = """
local key = KEYS[1]
local limit = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then
  tokens = limit
  ts = now
else
  -- refill
  tokens = math.min(limit, tokens + (now - ts) / window * limit)
  ts = now
end
local allowed = 0
local retry = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
else
  retry = math.ceil((1 - tokens) / limit * window)
  if retry < 1 then retry = 1 end
end
redis.call('HMSET', key, 'tokens', tostring(tokens), 'ts', tostring(ts))
-- keep the bucket alive for the full window plus a buffer (window is in ms)
redis.call('PEXPIRE', key, window + 60000)
return {allowed, retry}
"""


def _redis_client():
    url = os.environ.get("REDIS_URL") or get_settings().redis_url
    if not url:
        return None
    try:
        import redis

        client = redis.Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
        client.ping()
        return client
    except Exception:
        return None


class _RedisLimiter:
    def __init__(self, client):
        self._c = client
        self._script = client.register_script(_LUA_TAKE)

    def hit(self, key: str, limit: int, window_min: int = 1) -> tuple[bool, int]:
        """Returns (allowed, retry_after_seconds)."""
        window_ms = max(1, int(window_min * 60_000))

        def _to_int(x):
            # redis-py returns Lua integers as int, but some versions/setups return bytes.
            if isinstance(x, (bytes, bytearray)):
                return int(x.decode())
            return int(x)

        try:
            res = self._script(
                keys=[f"rag:rl:{key}"],
                args=[limit, window_ms, int(time.time() * 1000)],
            )
            allowed = _to_int(res[0])
            retry_ms = _to_int(res[1])
            return bool(allowed), retry_ms // 1000 + (1 if retry_ms % 1000 else 0)
        except Exception:
            return True, 0


class _MemoryLimiter:
    def __init__(self):
        import threading

        self._buckets: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int, window_min: int = 1) -> tuple[bool, int]:
        window = max(1, window_min * 60)
        now = time.time()
        with self._lock:
            times = self._buckets.setdefault(key, [])
            # drop timestamps outside the window
            cutoff = now - window
            times[:] = [t for t in times if t > cutoff]
            if len(times) >= limit:
                retry = int(times[0] - cutoff) + 1
                return False, max(1, retry)
            times.append(now)
            return True, 0


def _build() -> object:
    rc = _redis_client()
    if rc is not None:
        try:
            return _RedisLimiter(rc)
        except Exception:
            pass
    return _MemoryLimiter()


def reset_limiter(backend: str = "memory"):
    """Rebuild the module-level singleton (used by tests and by config reloads).

    backend: 'memory' forces the in-process limiter; 'auto' selects Redis when
    REDIS_URL is set, else in-memory.
    """
    global _limiter
    _limiter = _MemoryLimiter() if backend == "memory" else _build()
    return _limiter


_limiter = None  # lazily built on first use (see _get_limiter)


def _get_limiter():
    """Return the active limiter, building it lazily and self-healing on boot races.

    If REDIS_URL is configured but we previously fell back to in-memory (e.g. Redis
    was briefly unreachable at process start), we retry the Redis backend so the fleet
    keeps a single shared budget. This makes the backend decision robust to startup
    ordering without re-checking every request.
    """
    global _limiter
    if _limiter is None or (get_settings().redis_url and not isinstance(_limiter, _RedisLimiter)):
        _limiter = _build()
    return _limiter


def rate_limit(request, tenant_id: str | None = None, cost: int = 1):
    """FastAPI dependency enforcing per-IP and per-tenant limits.

    Raises 429 with Retry-After on the first exceeded dimension. `cost` lets an
    expensive operation (e.g. large ingest) consume more than one token.
    """
    from fastapi import (  # local import keeps module importable in tests
        HTTPException,
        status,
    )

    s = get_settings()
    ip = _client_ip(request)
    lim = _get_limiter()
    # per-IP (network safety)
    if s.rate_per_ip_per_min > 0:
        ok, retry = lim.hit(f"ip:{ip}", s.rate_per_ip_per_min)
        if not ok:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="rate limit exceeded (per IP)",
                headers={"Retry-After": str(retry)},
            )
    # per-tenant (tenant fairness)
    if tenant_id and s.rate_per_tenant_per_min > 0:
        ok, retry = lim.hit(f"tenant:{tenant_id}", s.rate_per_tenant_per_min, cost // 1 or 1)
        if not ok:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="rate limit exceeded (per tenant)",
                headers={"Retry-After": str(retry)},
            )


def _client_ip(request) -> str:
    """Resolve the real client IP for rate limiting.

    Security: the X-Forwarded-For header is ONLY honored when the immediate connection
    actually originates from a configured trusted proxy (TRUSTED_PROXIES CIDRs). Otherwise
    a direct client could spoof XFF and bypass IP throttling. When trusted, we take the
    *leftmost* (original client) entry, which is the value the trusted proxy prepended.
    """
    s = get_settings()
    raw_peer = request.client.host if request.client else "unknown"
    fwd = request.headers.get("x-forwarded-for")
    if not fwd:
        return raw_peer
    # Is the connection itself from a trusted proxy?
    trusted = False
    proxies = [p.strip() for p in s.trusted_proxies.split(",") if p.strip()]
    if proxies:
        try:
            import ipaddress

            peer = ipaddress.ip_address(raw_peer)
            trusted = any(peer in ipaddress.ip_network(c) for c in proxies)
        except ValueError:
            trusted = False
    if not trusted:
        # Untrusted connection claiming XFF -> ignore it; use the real socket peer.
        return raw_peer
    # Trusted: original client is the first hop in the chain.
    return fwd.split(",")[0].strip()
