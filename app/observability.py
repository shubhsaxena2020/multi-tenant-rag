"""Operability: structured logging + Prometheus metrics.

The service must be operable, not just functional. Two primitives:
- `get_logger()`: JSON-structured logs (tenant_id, trace_id, path, latency_ms when used via middleware).
- `METRICS`: Prometheus counters/histograms for request volume, latency, errors,
  ingestion throughput, and retrieval quality. Exposed at GET /metrics.
"""
from __future__ import annotations

import contextvars
import logging
import math
import re
import sys
import time
import uuid

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Histogram,
    Gauge,
    generate_latest,
)

# Context variables for logging
tenant_id_var: contextvars.ContextVar[str] = contextvars.ContextVar('tenant_id', default='')
trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar('trace_id', default='')

def generate_trace_id() -> str:
    """Generate a random trace ID."""
    return str(uuid.uuid4())

# ---------- Metrics (defined once at import) ----------
REQUEST_COUNT = Counter(
    "rag_requests_total", "Total HTTP requests", ["method", "path", "status"]
)
REQUEST_LATENCY = Histogram(
    "rag_request_duration_seconds", "HTTP request latency", ["method", "path"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
)
INGEST_JOBS = Counter(
    "rag_ingest_jobs_total", "Ingestion jobs by outcome", ["status"]
)

# v9-5: Job queue backend and status metrics for multi-replica safety
RAG_JOB_QUEUE_BACKEND = Gauge(
    "rag_job_queue_backend",
    "Current job queue backend mode",
    ["backend"],
)
RAG_JOB_STATUS_TOTAL = Counter(
    "rag_job_status_total",
    "Total jobs by status across all tenants",
    ["status"],
)

# Sitemap crawl latency — monitor-001
SITEMAP_CRAWL_LATENCY = Histogram(
    "rag_sitemap_crawl_duration_seconds",
    "Sitemap crawl latency by outcome",
    ["outcome"],
    buckets=(0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0),
)

# Duplicate detection — monitor-002
DUPLICATE_DETECTIONS = Counter(
    "rag_duplicate_detections_total",
    "Total duplicate URLs detected during ingestion",
    ["tenant_id"],
)

# Robots rate-limit honoring — monitor-003
ROBOTS_RATE_LIMIT = Counter(
    "rag_robots_rate_limit_exceeded_total",
    "Total robots rate-limit expirations",
    ["tenant_id"],
)

# Queue depth gauges — monitor-004
INGEST_QUEUE_DEPTH = Gauge(
    "rag_ingest_queue_depth",
    "Current number of jobs in the ingestion queue",
    ["backend"],
)
INGEST_CHUNKS = Counter(
    "rag_ingest_chunks_total", "Chunks embedded+stored"
)

# P2: release/ingestion incident visibility — operators can confirm
# incidents via /metrics; paired with an alert on sustained > 0.
RELEASE_INCIDENTS = Counter(
    "rag_release_incidents_total",
    "Release or ingestion incidents by outcome",
    ["outcome"],
)
RETRIEVAL_LATENCY = Histogram(
    "rag_retrieval_duration_seconds", "Retrieval (vector+rerank) latency",
    buckets=(0.02, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)
QUERY_HITS = Histogram(
    "rag_query_hits", "Number of chunks returned per query",
    buckets=(0, 1, 3, 5, 10, 20, 50),
)
REWRITE_USED = Counter(
    "rag_query_rewrite_total", "Pre-retrieval query rewrites by outcome", ["used"]
)
FAITHFULNESS_SCORE = Histogram(
    "rag_faithfulness", "Answer faithfulness (grounding) score per generated answer",
    buckets=(0.0, 0.2, 0.4, 0.6, 0.8, 1.01)
)
NO_ANSWER_TOTAL = Counter(
    "rag_no_answer_total", "Queries that returned a safe no-answer (unanswerable / out of scope)"
)

# P1: timeout/no-response rate — distinct from safe no-answer
RESPONSE_TOTAL = Counter(
    "rag_no_response_total", "Queries that returned no response / timed out"
)

# ---------------- v9-5: SLO tracking ----------------
# Availability = successful (non-5xx) responses / total. Latency SLO = p95 under target.
SLO_AVAILABILITY = Counter(
    "rag_slo_requests_total", "Requests for SLO availability (split by ok/error)", ["outcome"]
)
SLO_LATENCY_OBS = Histogram(
    "rag_slo_latency_seconds", "Request latency used for the latency SLO",
    buckets=(0.05, 0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0),
)
DEGRADED_RESPONSES = Counter(
    "rag_degraded_responses_total", "Responses served in degraded mode (backend impairment)",
    ["path"]
)
CIRCUIT_OPEN_EVENTS = Counter(
    "rag_circuit_open_total", "Times a circuit breaker opened (dependency unhealthy)", ["dependency"]
)


# P1 #6: audit-write failures are fail-open (we never block the primary op), but a gap
# in the accountability trail must NOT go unnoticed. This counter makes a silently-dropped
# audit write observable to Prometheus/Alertmanager; pair with an alert on sustained > 0.
AUDIT_FAILURES = Counter(
    "rag_audit_write_failures_total", "Audit log appends that failed (dropped, fail-open)",
    ["action"]
)


def record_slo(method: str, path: str, status: int, latency: float) -> None:
    """Update SLO counters/histograms for one completed request (v9-5)."""
    ok = 200 <= status < 500
    SLO_AVAILABILITY.labels(outcome="ok" if ok else "error").inc()
    SLO_LATENCY_OBS.observe(latency)
    if status >= 500:
        DEGRADED_RESPONSES.labels(path=path).inc()


def record_release_incident(outcome: str = "success") -> None:
    """Record a release or ingestion incident for operators to confirm via /metrics."""
    RELEASE_INCIDENTS.labels(outcome=outcome).inc()


def _metric_value(sample) -> float:
    """prometheus_client stores the live value on `._value`; older versions expose it as a
    Float with `.get()`, newer ones as a plain float. Normalize both."""
    v = getattr(sample, "_value", None)
    if v is None:
        return 0.0
    if hasattr(v, "get"):
        return float(v.get())
    return float(v)


def compute_slo_status(target_latency_p95: float, target_availability: float) -> dict:
    """Compute current SLO status from Prometheus metric state (v9-5).

    Uses in-process counters so it works without a Prometheus query backend; the same
    data is also exported as metrics for a real Prometheus/Alertmanager stack."""
    ok = _metric_value(SLO_AVAILABILITY.labels(outcome="ok"))
    err = _metric_value(SLO_AVAILABILITY.labels(outcome="error"))
    total = ok + err
    availability = (ok / total) if total else 1.0
    p95 = _histogram_p95(SLO_LATENCY_OBS) or 0.0
    if p95 == float("inf") or math.isnan(p95):  # inf or nan -> no data yet
        p95 = 0.0
    return {
        "availability": round(availability, 4),
        "availability_target": target_availability,
        "availability_met": availability >= target_availability,
        "latency_p95_s": round(p95, 3),
        "latency_target_s": target_latency_p95,
        "latency_met": p95 <= target_latency_p95,
        "total_requests": int(total),
    }


def _histogram_p95(hist) -> float | None:
    """Approximate p95 from a prometheus histogram's cumulative buckets."""
    buckets = getattr(hist, "_buckets", None)
    if not buckets:
        return None
    counts = [_metric_value(b) for b in buckets]
    total = sum(counts)
    if total == 0:
        return None
    target = total * 0.95
    cum = 0.0
    for i, c in enumerate(counts):
        cum += c
        if cum >= target:
            return float(getattr(buckets[i], "upper_bound", float("inf")))
    return float(getattr(buckets[-1], "upper_bound", float("inf")))

# ---------- Structured logging ----------
def get_logger(name: str = "rag") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        formatter = logging.Formatter(
            '{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","tenant_id":"%(tenant_id)s","trace_id":"%(trace_id)s","msg":"%(message)s"}'
        )
        # Add a filter to inject tenant_id and trace_id from contextvars
        class ContextFilter(logging.Filter):
            def filter(self, record: logging.LogRecord) -> bool:
                record.tenant_id = tenant_id_var.get('')
                record.trace_id = trace_id_var.get('')
                return True
        handler.addFilter(ContextFilter())
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


class MetricsMiddleware:
    """Record per-request count + latency. Path is normalized to avoid label explosion
    (template path, not raw URL) so per-tenant/per-doc/per-job IDs in the URL don't
    create an unbounded number of Prometheus series (a classic OOM footgun).
    """

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

        # Extract tenant_id from Authorization header if present and valid for logging
        tenant_id = ""
        headers = dict(scope.get("headers", []))
        if b"authorization" in headers:
            auth_header = headers[b"authorization"].decode()
            if auth_header.startswith("Bearer "):
                key = auth_header.split(" ", 1)[1].strip()
                try:
                    # Import here to avoid circular dependency
                    from . import tenants
                    tenant = await tenants.get_tenant_by_key(key)
                    if tenant is not None:
                        tenant_id = tenant.tenant_id
                except Exception:
                    # If we fail, leave tenant_id as empty string
                    pass

        # Set context variables for logging
        tenant_id_token = tenant_id_var.set(tenant_id)
        trace_id_token = trace_id_var.set(generate_trace_id())

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
            # v9-5: feed SLO availability + latency tracking
            record_slo(method, label_path, status, time.perf_counter() - start)
            # Record no-response metric: queries that returned no response / timed out
            # This feeds the RAGNoResponseRate alert (rag_no_response_total / rag_requests_total > 0.15)
            if status >= 500:
                RESPONSE_TOTAL.inc()
            # Reset context variables
            tenant_id_var.reset(tenant_id_token)
            trace_id_var.reset(trace_id_token)


def metrics_response() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
