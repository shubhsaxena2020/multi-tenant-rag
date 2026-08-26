"""Operability: structured logging + Prometheus metrics.

The service must be operable, not just functional. Two primitives:
- `get_logger()`: JSON-structured logs (tenant_id, path, latency_ms when used via middleware).
- `METRICS`: Prometheus counters/histograms for request volume, latency, errors,
  ingestion throughput, and retrieval quality. Exposed at GET /metrics.
"""
from __future__ import annotations

import logging
import re
import sys
import time

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Histogram,
    generate_latest,
)

# ---------- Metrics (defined once at import) ----------
REQUEST_COUNT = Counter(
    "rag_requests_total", "Total HTTP requests", ["method", "path", "status"]
)
REQUEST_LATENCY = Histogram(
    "rag_request_duration_seconds", "HTTP request latency", ["method", "path"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
)
INGEST_JOBS = Counter(
    "rag_ingest_jobs_total", "Ingestion jobs by outcome", ["tenant_id", "status"]
)
INGEST_CHUNKS = Counter(
    "rag_ingest_chunks_total", "Chunks embedded+stored", ["tenant_id"]
)
RETRIEVAL_LATENCY = Histogram(
    "rag_retrieval_duration_seconds", "Retrieval (vector+rerank) latency", ["tenant_id"],
    buckets=(0.02, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)
QUERY_HITS = Histogram(
    "rag_query_hits", "Number of chunks returned per query", ["tenant_id"],
    buckets=(0, 1, 3, 5, 10, 20, 50),
)


def metrics_response() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST


# ---------- Structured logging ----------
def get_logger(name: str = "rag") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter('{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}')
        )
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


class MetricsMiddleware:
    """Record per-request count + latency. Path is normalized to avoid label explosion
    (template path, not raw URL) so per-tenant/per-doc/per-job IDs in the URL don't
    create an unbounded number of Prometheus series (a classic OOM footgun)."""

    # Any segment that looks like an id/uuid is collapsed to a template token.
    _ID_RE = re.compile(
        r"^(t_[0-9a-f]{8,}|d_[0-9a-f]{8,}|job_[0-9a-f]{8,}|run_[0-9a-f]{8,}|"
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|"
        r"[0-9a-zA-Z_-]{16,})$"
    )

    def _normalize(self, path: str) -> str:
        parts = [p for p in path.split("/") if p != ""]
        out: list[str] = []
        for p in parts:
            if self._ID_RE.match(p):
                out.append("{id}")
            else:
                out.append(p)
        norm = "/" + "/".join(out) if out else "/"
        # Map known API prefixes to their canonical template (so e.g. /api/v1/t_xxx ->
        # /api/v1/{tenant}) even when the id token differs from the documented form.
        if norm.startswith("/api/v1/"):
            rest = norm[len("/api/v1/"):]
            segs = rest.split("/") if rest else []
            if segs and segs[0] == "{id}":
                segs[0] = "{tenant}"
                norm = "/api/v1/" + "/".join(segs)
        return norm

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "GET")
        # Normalize the raw path to a template so per-tenant/per-doc/per-job IDs in the
        # URL don't explode metric label cardinality. We intentionally do NOT rely on
        # scope["route"] (not populated here); we normalize deterministically instead.
        raw_path = scope.get("path", "/")
        label_path = self._normalize(raw_path)
        start = time.perf_counter()
        status = 500

        def _send_wrapper(message):
            nonlocal status
            if message.get("type") == "http.response.start":
                status = message.get("status", 500)
            return send(message)

        try:
            await self.app(scope, receive, _send_wrapper)
        finally:
            REQUEST_COUNT.labels(method=method, path=label_path, status=status).inc()
            REQUEST_LATENCY.labels(method=method, path=label_path).observe(time.perf_counter() - start)
