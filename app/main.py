"""FastAPI application: multi-tenant RAG service, versioned under /api/v1.

Isolation: tenant identity is derived server-side from the Bearer API key on every
request (never from the request body). All vector operations are scoped to the
tenant's own Qdrant collection, so cross-tenant reads are structurally impossible.

Versioning: all tenant-facing routes live under /api/v1 (mounted sub-app). The
OpenAPI schema is served at /api/v1/openapi.json and interactive docs at /api/v1/docs.
Operability: structured JSON logging + Prometheus metrics at /metrics; per-tenant and
per-IP rate limiting on every tenant route.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from . import jobs as job_store
from . import tenants
from .audit import append_audit, list_audit, verify_chain
from .auth import (
    generate_api_key,
    generate_publishable_key,
    get_tenant_from_header,
    require_admin,
    require_secret_key,
)
from .config import get_settings
from .conversation import (
    assess_confidence,
    detect_injection,
    get_session_store,
    rewrite_query,
)
from .generation import generate_answer, stream_answer
from .ingestion import ingest_text, ingest_url
from .ingestion.runner import submit
from .models import (
    DocumentCatalogOut,
    DocumentCreate,
    DocumentOut,
    EvalReportOut,
    EvalSetIn,
    IngestJobRequest,
    IngestText,
    IngestUrl,
    JobStatus,
    KeyInfo,
    QueryRequest,
    QueryResponse,
    RetrievedChunk,
    SitemapIngestIn,
    SitemapJobOut,
    TenantCreate,
    TenantKeysOut,
    TenantOut,
    UploadOut,
)
from .observability import (
    INGEST_CHUNKS,
    INGEST_JOBS,
    QUERY_HITS,
    RETRIEVAL_LATENCY,
    MetricsMiddleware,
    get_logger,
    metrics_response,
)
from .ratelimit import rate_limit
from .rbac import (
    PUBLIC_GROUP,
    build_acl_filter,
    resolve_acl,
)
from .resilience import RagError, circuit_status
from .retrieval import retrieve
from .validation import (
    validate_content,
    validate_content_type,
    validate_metadata,
)
from .vector_store import (
    delete_document,
    delete_tenant_collection,
    ensure_collection,
    get_client,
)

log = get_logger("rag")

# Public root app (health, metrics, docs). Tenant API mounted at /api/v1.
from contextlib import asynccontextmanager


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # v9-5: durable job recovery — reset any jobs orphaned in `running` by a previous
    # crashed worker back to `pending` so they get retried.
    try:
        from .db import init_db, requeue_orphaned_jobs

        await init_db()
        recovered = await requeue_orphaned_jobs()
        if recovered:
            log.info("jobs_recovered_on_startup", extra={"recovered": recovered})
    except Exception as e:
        log.warning("startup_recovery_failed", extra={"error_type": type(e).__name__})
    yield


app = FastAPI(
    lifespan=_lifespan,
    title="RAG Service",
    version="1.0.0",
    description="Multi-tenant Retrieval-Augmented Generation service. Tenant API is "
    "versioned under /api/v1. See /api/v1/docs for the interactive contract.",
    openapi_url=None,  # root has no schema; /api/v1 owns the versioned contract
)
# ---------------- Safe CORS (deny-by-default, allowlist-only) ----------------
# The embeddable widget (sdk.py / /widget.js) is loaded cross-origin inside a
# client's own site, so the browser needs CORS to call /api/v1/{tenant}/query[/stream].
# A naive `allow_origins=["*"]` would let ANY website drive a tenant's API with a
# victim's key. We therefore mirror the `allowed_embed_origins` allowlist (also used
# for CSP frame-ancestors) and DENY everything else. The matched Origin is echoed
# EXACTLY (never a wildcard, never an arbitrary client-supplied value), and we never
# set Access-Control-Allow-Credentials — auth is via the Authorization header, not
# cookies, so cross-origin credentialed requests are not needed and must stay blocked.
_CORS_ALLOW_HEADERS = ("Authorization", "Content-Type", "Admin-Key")
_CORS_ALLOW_METHODS = ("GET", "POST", "DELETE", "OPTIONS")


class CORSMiddleware:
    """Allowlist-only CORS. No Origin is ever reflected unless explicitly configured."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        origin = ""
        for raw_key, raw_val in scope.get("headers", []):
            if raw_key == b"origin":
                origin = raw_val.decode("latin-1")
                break
        allowed = get_settings().allowed_embed_origins
        allow = bool(origin) and origin in allowed

        if scope.get("method") == "OPTIONS":
            # Preflight: only respond when the Origin is explicitly allowlisted.
            if allow:
                await self._send_preflight(send, origin)
            else:
                await _send_status(send, 403, b"")
            return

        if allow:
            async def _wrap(message):
                if message["type"] == "http.response.start":
                    headers = list(message.get("headers", []))
                    headers.append((b"access-control-allow-origin", origin.encode("latin-1")))
                    message["headers"] = headers
                await send(message)

            await self.app(scope, receive, _wrap)
        else:
            await self.app(scope, receive, send)

    async def _send_preflight(self, send, origin):
        headers = [
            (b"access-control-allow-origin", origin.encode("latin-1")),
            (
                b"access-control-allow-methods",
                b",".join(m.encode() for m in _CORS_ALLOW_METHODS),
            ),
            (
                b"access-control-allow-headers",
                b",".join(h.encode() for h in _CORS_ALLOW_HEADERS),
            ),
            (b"access-control-max-age", b"600"),
            (b"content-length", b"0"),
        ]
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        await send({"type": "http.response.body", "body": b""})


async def _send_status(send, status, body):
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-length", str(len(body)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": body})


app.add_middleware(CORSMiddleware)
app.add_middleware(MetricsMiddleware)


# ---------------- Global exception handlers (v9-1 graceful degradation) ----------------
# A Qdrant/embedder/reranker outage used to produce raw HTTP 500 stack traces on every
# chatbot query. These handlers convert backend failures into a clean, friendly response
# carrying `degraded: true` instead of crashing — so client chatbots look professional
# during any infra hiccup.
@app.exception_handler(RagError)
async def _rag_error_handler(request: Request, exc: RagError):
    log.warning(
        "rag_error",
        extra={"path": request.url.path, "error_type": type(exc).__name__,
               "detail": exc.public_detail},
    )
    status_code = 503 if exc.degraded else 500
    return JSONResponse(
        status_code=status_code,
        content={
            "error": exc.public_detail,
            "degraded": exc.degraded,
            "detail": exc.public_detail,
        },
    )


@app.exception_handler(Exception)
async def _unhandled_error_handler(request: Request, exc: Exception):
    # Last-resort: never leak raw exception text/stack to clients. Log type only.
    if isinstance(exc, RagError):
        return await _rag_error_handler(request, exc)
    log.error("unhandled_error", extra={"path": request.url.path, "error_type": type(exc).__name__})
    return JSONResponse(
        status_code=500,
        content={"error": "internal error", "degraded": False,
                 "detail": type(exc).__name__},
    )

v1 = FastAPI(
    title="RAG Service API",
    version="1.0.0",
    root_path="/api/v1",  # so the generated OpenAPI + Swagger reflect the real mounted URL
    description=(
        "Versioned multi-tenant RAG API. Every tenant route requires "
        "`Authorization: Bearer *** Tenant identity is resolved "
        "server-side from the key; the {tenant} path segment is informational."
    ),
    # E: docs/openapi are unauthenticated by default in FastAPI — disable the built-in
    # endpoints and serve them only via the admin-gated routes below so they don't
    # disclose tenant-id/path/schema info to the public.
    openapi_url=None,
    docs_url=None,
    redoc_url=None,
)


TenantDep = Annotated[tenants.TenantRow, Depends(get_tenant_from_header)]


async def audit_event(action: str, actor: str, target: str = "", meta: dict | None = None) -> None:
    """Best-effort, fail-open audit append. Never raises (see app/audit.py).

    `actor` is always server-derived (admin key fingerprint or 'system'), never the
    untrusted request body — so the audit trail itself can't be spoofed by a caller.
    """
    # Hash the actor to a stable, non-secret fingerprint. Applies to EVERY non-system
    # actor (short OR long) so a short ADMIN_API_KEY is never written to the audit table
    # in plaintext (P1 #3). `system` is the only literal passthrough (not a secret).
    if actor and actor != "system":
        actor = "admin:" + hashlib.sha256(actor.encode()).hexdigest()[:16]
    await append_audit(action, actor, target=target, meta=meta)


async def _audit_data_plane(action: str, tenant_id: str, target: str = "", meta: dict | None = None) -> None:
    """Sampled, fail-open audit of tenant data-plane actions (ingest/query/delete/eval).

    P1 #5: the accountability trail previously only covered admin actions. High-volume
    data-plane events are sampled (config.audit_sample_rate) so the trail is observable
    without writing every row. The tenant_id (a non-secret opaque id) is the actor; never
    the caller-supplied body. We await append_audit directly (it is itself fail-open and
    never raises) so the write is deterministic and testable; the cost is one sampled
    sqlite insert per event — sub-millisecond, and reduced further by lowering
    audit_sample_rate in high-throughput deployments.
    """
    import random

    s = get_settings()
    if s.audit_sample_rate <= 0:
        return
    if random.random() >= s.audit_sample_rate:
        return
    await append_audit(action, "tenant:" + tenant_id, target=target, meta=meta or {})


def _resolved_acl(requested: list[str] | None, auth: tenants.TenantRow, *, default_to_public: bool) -> list[str]:
    """Server-side RBAC: narrow caller-requested groups to what the tenant is provisioned
    for. Unauthorized groups are dropped and surfaced in telemetry (self-escalation attempt).
    Returns the concrete acl list to apply (never None)."""
    eff = resolve_acl(requested, auth.allowed_groups, default_to_public=default_to_public)
    if requested is not None:
        dropped = [g for g in requested if g not in eff and g != PUBLIC_GROUP]
        if dropped:
            log.warning(
                "rbac_self_escalation_blocked",
                extra={"tenant_id": auth.tenant_id, "dropped_groups": dropped,
                       "allowed": auth.allowed_groups},
            )
    return eff


def _parse_json_field(raw: str | None) -> dict:
    """Parse an optional JSON-string form field into a dict (empty dict if absent/invalid)."""
    if not raw:
        return {}
    import json

    try:
        val = json.loads(raw)
    except Exception:
        return {}
    return val if isinstance(val, dict) else {}


def _parse_acl_field(raw: str | None) -> list[str] | None:
    """Parse an optional JSON-array form field into a list of groups (None if absent)."""
    if not raw:
        return None
    import json

    try:
        val = json.loads(raw)
    except Exception:
        return None
    return val if isinstance(val, list) else None


# ---------------- Root (operability) routes ----------------
@app.get("/health")
def health():
    return {"status": "ok", "service": "rag-service", "version": "1.0.0"}


@app.get("/health/deps")
def health_deps():
    """Dependency/circuit-breaker status for ops dashboards (v9-1 resilience)."""
    return {
        "status": "ok",
        "circuit_breakers": {
            "qdrant": circuit_status("qdrant"),
            "embedder": circuit_status("embedder"),
            "reranker": circuit_status("reranker"),
        },
    }


@app.get("/health/ready")
def ready():
    try:
        c = get_client()
        c.get_collections()
        return {"status": "ready", "qdrant": "reachable"}
    except Exception as exc:
        # Never leak raw backend exception text/stack to clients (G: log sanitization).
        log.warning("qdrant_unreachable", extra={"error_type": type(exc).__name__})
        raise HTTPException(status_code=503, detail="qdrant unreachable")


# ---------------- v9-3: embeddable widget ----------------
def _frame_ancestors_csp() -> str:
    """CSP frame-ancestors from allowed_embed_origins. Empty list => 'none' (no embedding)."""
    origins = get_settings().allowed_embed_origins
    if not origins:
        return "frame-ancestors 'none'"
    return "frame-ancestors " + " ".join(origins)


@app.get("/widget.js")
def widget_js():
    from pathlib import Path

    p = Path(__file__).parent / "static" / "widget.js"
    body = p.read_text(encoding="utf-8")
    return HTMLResponse(body, media_type="application/javascript",
                        headers={"Content-Security-Policy": _frame_ancestors_csp()})


@app.get("/widget.html")
def widget_html():
    from pathlib import Path

    p = Path(__file__).parent / "static" / "widget.html"
    body = p.read_text(encoding="utf-8")
    # The iframe itself is locked to allowed embed origins; the inner page gets a
    # permissive-enough CSP for its own scripts but no frame nesting beyond what we set.
    return HTMLResponse(body, media_type="text/html",
                        headers={"Content-Security-Policy": _frame_ancestors_csp() + "; default-src 'self' 'unsafe-inline'"})


@app.get("/health/slo")
def health_slo():
    """v9-5: current SLO status (availability + p95 latency) vs configured targets."""
    from .config import get_settings
    from .observability import compute_slo_status

    s = get_settings()
    status = compute_slo_status(s.slo_latency_p95_s, s.slo_availability)
    status["status"] = "ok" if (status["availability_met"] and status["latency_met"]) else "breach"
    return status


@app.get("/metrics")
def metrics(_: None = Depends(require_admin)):
    body, ctype = metrics_response()
    return body, {"content-type": ctype}


# E: admin-gated OpenAPI schema + interactive docs. Served on the root app (not the
# public v1 sub-app) so only an operator with the Admin-Key can fetch the contract.
@app.get("/api/v1/openapi.json")
def openapi_schema(_: None = Depends(require_admin)):
    return v1.openapi()


@app.get("/api/v1/docs", include_in_schema=False)
def docs(_: None = Depends(require_admin)):
    from fastapi.openapi.docs import get_swagger_ui_html

    return get_swagger_ui_html(openapi_url="/api/v1/openapi.json", title="RAG Service API")


# ---------------- Admin: tamper-evident audit log ----------------
@app.get("/audit")
async def audit_log(_: None = Depends(require_admin), limit: int = 200):
    """Read the hash-chained audit trail. Admin-gated (fail-closed like /metrics)."""
    rows = await list_audit(limit=limit)
    return {"entries": rows, "count": len(rows)}


@app.get("/audit/verify")
async def audit_verify(_: None = Depends(require_admin)):
    """Verify chain integrity. `ok=False` + `first_break_id` indicates tampering."""
    return await verify_chain()


# ---------------- Admin: tenants ----------------
@v1.post("/tenants", response_model=TenantOut, status_code=status.HTTP_201_CREATED)
async def create_tenant(body: TenantCreate, request: Request, _: None = Depends(require_admin)):
    admin_key = request.headers.get("Admin-Key") or request.headers.get("Authorization", "")
    tenant_id = f"t_{uuid.uuid4().hex[:12]}"
    api_key = generate_api_key()
    row = await tenants.create_tenant(body.name, tenant_id, api_key, body.plan, body.allowed_groups)
    try:
        ensure_collection(get_client())
    except Exception as exc:
        log.warning("ensure_collection failed for %s: %s", tenant_id, exc)
    log.info("tenant_created", extra={"tenant_id": tenant_id, "tenant_name": body.name})
    await audit_event(
        "tenant.create", actor=admin_key, target=tenant_id,
        meta={"name": body.name, "plan": body.plan, "allowed_groups": body.allowed_groups},
    )
    return TenantOut(
        tenant_id=row.tenant_id, name=row.name, api_key=api_key,
        plan=row.plan, created_at=row.created_at, chunk_count=row.chunk_count,
        allowed_groups=row.allowed_groups,
    )


@v1.get("/tenants", response_model=list[TenantOut])
async def list_tenants(_: None = Depends(require_admin)):
    return [
        TenantOut(
            tenant_id=t.tenant_id, name=t.name, api_key=f"{t.api_key_prefix}...",
            plan=t.plan, created_at=t.created_at, chunk_count=t.chunk_count,
        )
        for t in await tenants.list_tenants()
    ]


@v1.delete("/tenants/{tenant_id}", status_code=status.HTTP_200_OK)
async def delete_tenant(tenant_id: str, request: Request, _: None = Depends(require_admin)):
    """Offboard a tenant: drop its Qdrant collection (hard data removal) and remove
    the registry row. This guarantees no residual vectors remain."""
    admin_key = request.headers.get("Admin-Key") or request.headers.get("Authorization", "")
    dropped = delete_tenant_collection(tenant_id)
    removed = await tenants.delete_tenant(tenant_id)
    if not removed:
        raise HTTPException(status_code=404, detail="tenant not found")
    log.info("tenant_offboarded", extra={"tenant_id": tenant_id, "collection_dropped": dropped})
    await audit_event(
        "tenant.delete", actor=admin_key, target=tenant_id,
        meta={"collection_dropped": dropped},
    )
    return {"deleted": tenant_id, "collection_dropped": dropped}


# ---------------- Synchronous ingestion (convenience, small payloads) ----------------
@v1.post("/{tenant}/documents", response_model=DocumentOut, status_code=status.HTTP_201_CREATED)
async def create_document(tenant: str, body: DocumentCreate, request: Request, auth: TenantDep, _: None = Depends(require_secret_key)):
    rate_limit(request, auth.tenant_id)
    validate_content(body.content)
    ct = validate_content_type(body.content_type)
    meta = validate_metadata(body.metadata)
    acl = _resolved_acl(body.acl, auth, default_to_public=True)
    res = await ingest_text(auth.tenant_id, body.title, body.content, ct, meta, acl=acl,
                            doc_id=body.doc_id)
    INGEST_CHUNKS.inc(res["chunk_count"])
    INGEST_JOBS.labels(status="success").inc()
    log.info("document_ingested", extra={"tenant_id": auth.tenant_id, "chunk_count": res["chunk_count"]})
    await _audit_data_plane(
        "tenant.ingest", auth.tenant_id, target=res["doc_id"],
        meta={"title": body.title, "chunks": res["chunk_count"],
              "quarantined": res["quarantined_chunks"], "kind": "document"},
    )
    return DocumentOut(
        doc_id=res["doc_id"], title=res["title"], chunk_count=res["chunk_count"],
        content_type=ct, metadata=meta or {}, quarantined_chunks=res["quarantined_chunks"],
    )


@v1.post("/{tenant}/ingest/url", response_model=DocumentOut, status_code=status.HTTP_201_CREATED)
async def ingest_from_url(tenant: str, body: IngestUrl, auth: TenantDep, request: Request, _: None = Depends(require_secret_key)):
    rate_limit(request, auth.tenant_id)
    meta = validate_metadata(body.metadata)
    acl = _resolved_acl(body.acl, auth, default_to_public=True)
    res = await ingest_url(auth.tenant_id, body.url, body.title, meta, acl=acl,
                           doc_id=body.doc_id)
    INGEST_CHUNKS.inc(res["chunk_count"])
    INGEST_JOBS.labels(status="success").inc()
    await _audit_data_plane(
        "tenant.ingest", auth.tenant_id, target=res["doc_id"],
        meta={"title": body.title, "chunks": res["chunk_count"],
              "quarantined": res["quarantined_chunks"], "kind": "url"},
    )
    return DocumentOut(
        doc_id=res["doc_id"], title=res["title"], chunk_count=res["chunk_count"],
        content_type="html", metadata=meta or {}, quarantined_chunks=res["quarantined_chunks"],
    )


@v1.post("/{tenant}/ingest/text", response_model=DocumentOut, status_code=status.HTTP_201_CREATED)
async def ingest_from_text(tenant: str, body: IngestText, auth: TenantDep, request: Request, _: None = Depends(require_secret_key)):
    rate_limit(request, auth.tenant_id)
    validate_content(body.text)
    ct = validate_content_type(body.content_type)
    meta = validate_metadata(body.metadata)
    acl = _resolved_acl(body.acl, auth, default_to_public=True)
    res = await ingest_text(auth.tenant_id, body.title, body.text, ct, meta, acl=acl,
                             doc_id=body.doc_id)
    INGEST_CHUNKS.inc(res["chunk_count"])
    INGEST_JOBS.labels(status="success").inc()
    await _audit_data_plane(
        "tenant.ingest", auth.tenant_id, target=res["doc_id"],
        meta={"title": body.title, "chunks": res["chunk_count"],
              "quarantined": res["quarantined_chunks"], "kind": "text"},
    )
    return DocumentOut(
        doc_id=res["doc_id"], title=res["title"], chunk_count=res["chunk_count"],
        content_type=ct, metadata=meta or {}, quarantined_chunks=res["quarantined_chunks"],
    )


# ---------------- Async ingestion jobs (canonical, status-tracked) ----------------
@v1.post("/{tenant}/ingest/jobs", response_model=JobStatus, status_code=status.HTTP_202_ACCEPTED)
async def create_ingest_job(tenant: str, body: IngestJobRequest, auth: TenantDep, request: Request, _: None = Depends(require_secret_key)):
    rate_limit(request, auth.tenant_id)
    s = get_settings()
    # protect the embedding worker pool. NOTE: ingest limit is per-MINUTE, so window_min=1
    # (the bug here previously passed rate_ingest_jobs_per_min as window_min, enforcing
    # ~1 job/hour). Fixed via explicit named args so this class of bug can't recur.
    from fastapi import HTTPException
    from fastapi import status as _st

    from .ratelimit import _limiter
    allowed, retry = _limiter.hit(
        f"ingest:{auth.tenant_id}", limit=s.rate_ingest_jobs_per_min, window_min=1
    )
    if not allowed:
        raise HTTPException(
            status_code=_st.HTTP_429_TOO_MANY_REQUESTS,
            detail="ingest job rate limit exceeded (per tenant, per minute)",
            headers={"Retry-After": str(retry)},
        )
    kind = body.kind
    title = body.title
    acl = _resolved_acl(body.acl, auth, default_to_public=True)
    if kind == "url":
        if not body.url:
            raise HTTPException(422, "url is required for kind=url")
        payload = {"url": body.url, "title": title, "acl": acl}
    elif kind == "text":
        if not body.text:
            raise HTTPException(422, "text is required for kind=text")
        validate_content(body.text)
        payload = {"text": body.text, "title": title, "content_type": body.content_type, "acl": acl}
    elif kind == "document":
        if not body.content:
            raise HTTPException(422, "content is required for kind=document")
        validate_content(body.content)
        payload = {"content": body.content, "title": title, "content_type": body.content_type, "acl": acl}
    else:
        raise HTTPException(422, f"unknown kind: {kind}")
    validate_content_type(body.content_type)
    meta = validate_metadata(body.metadata)
    job_id = await job_store.create_job(auth.tenant_id, kind, title or (body.url or "untitled"))
    INGEST_JOBS.labels(status="pending").inc()
    submit(job_id, auth.tenant_id, kind, payload, meta)
    log.info("ingest_job_submitted", extra={"tenant_id": auth.tenant_id, "job_id": job_id, "kind": kind})
    return JobStatus(
        job_id=job_id, tenant_id=auth.tenant_id, kind=kind, status="pending",
        progress=0.0, total_chunks=0, done_chunks=0, title=title,
    )


@v1.get("/{tenant}/jobs", response_model=list[JobStatus])
async def list_ingest_jobs(tenant: str, auth: TenantDep, request: Request, _: None = Depends(require_secret_key), limit: int = 50):
    rate_limit(request, auth.tenant_id)
    return [JobStatus(**j) for j in await job_store.list_jobs(auth.tenant_id, limit)]


@v1.get("/{tenant}/jobs/{job_id}", response_model=JobStatus)
async def get_ingest_job(tenant: str, job_id: str, auth: TenantDep, request: Request, _: None = Depends(require_secret_key)):
    rate_limit(request, auth.tenant_id)
    j = await job_store.get_job(job_id, auth.tenant_id)
    if j is None:
        raise HTTPException(status_code=404, detail="job not found")
    return JobStatus(**j)


@v1.delete("/{tenant}/jobs/{job_id}", status_code=status.HTTP_200_OK)
async def delete_ingest_job(tenant: str, job_id: str, auth: TenantDep, request: Request, _: None = Depends(require_secret_key)):
    rate_limit(request, auth.tenant_id)
    ok = await job_store.delete_job(job_id, auth.tenant_id)
    if not ok:
        raise HTTPException(status_code=404, detail="job not found")
    return {"deleted": job_id}


# ---------------- PHASE B.2: sitemap crawler (async job) ----------------
@v1.post("/{tenant}/ingest/sitemap", response_model=SitemapJobOut, status_code=status.HTTP_202_ACCEPTED)
async def create_sitemap_job(tenant: str, body: SitemapIngestIn, auth: TenantDep, request: Request, _: None = Depends(require_secret_key)):
    """Onboard a client site by crawling its sitemap.xml. Returns a job_id to poll.

    Respectful crawler: honors robots.txt (Allow/Disallow + Crawl-delay), caps
    concurrency and total URLs, and reuses the SSRF-safe fetcher. Re-crawls are
    idempotent (each URL is upserted under a stable source_hash), so re-running a
    sitemap REPLACES changed pages instead of duplicating them (PHASE B.1).
    """
    rate_limit(request, auth.tenant_id)
    from fastapi import status as _st

    from .ratelimit import _limiter

    s = get_settings()
    allowed, retry = _limiter.hit(f"ingest:{auth.tenant_id}", limit=s.rate_ingest_jobs_per_min, window_min=1)
    if not allowed:
        raise HTTPException(
            status_code=_st.HTTP_429_TOO_MANY_REQUESTS,
            detail="ingest job rate limit exceeded (per tenant, per minute)",
            headers={"Retry-After": str(retry)},
        )
    job_title = body.title or f"sitemap:{body.url}"
    job_id = await job_store.create_job(auth.tenant_id, "sitemap", job_title)
    INGEST_JOBS.labels(status="pending").inc()
    meta = validate_metadata(body.metadata)
    acl = _resolved_acl(body.acl, auth, default_to_public=True)
    # Run the crawl in a detached background task (independent of the request).
    import asyncio

    async def _run():
        await job_store.update_job(job_id, status="running", progress=0.05)
        try:
            from .ingestion.sitemap import crawl_sitemap

            res = await crawl_sitemap(
                auth.tenant_id, body.url,
                max_urls=body.max_urls, concurrency=body.concurrency,
                acl=acl, metadata=meta,
                on_progress=lambda done, total: asyncio.create_task(
                    job_store.update_job(job_id, progress=0.1 + 0.9 * (done / total))
                ),
            )
            await job_store.update_job(
                job_id, status="completed", progress=1.0,
                result_doc_id=None,
                total_chunks=res.urls_ingested,
                done_chunks=res.urls_ingested,
            )
            # Persist crawl stats onto the job row via a small side-table update.
            await _save_sitemap_stats(job_id, res)
            INGEST_JOBS.labels(status="completed").inc()
            log.info("sitemap_crawl_done",
                     extra={"tenant_id": auth.tenant_id, "job_id": job_id,
                            "ingested": res.urls_ingested, "failed": res.urls_failed})
        except Exception as e:
            safe_err = f"{type(e).__name__}: {str(e)[:200]}"
            await job_store.update_job(job_id, status="failed", error=safe_err)
            INGEST_JOBS.labels(status="failed").inc()
            log.error("sitemap_crawl_failed",
                      extra={"tenant_id": auth.tenant_id, "job_id": job_id, "error": safe_err})

    asyncio.create_task(_run())
    return SitemapJobOut(
        job_id=job_id, tenant_id=auth.tenant_id, status="pending",
        kind="sitemap", title=job_title,
    )


async def _save_sitemap_stats(job_id: str, res) -> None:
    """Best-effort: stash crawl counters on the job row (reuses the error/title fields)."""
    try:
        from .jobs import update_job
        # Encode crawl summary into the job title suffix (surfaced by GET /jobs).
        # (The Job model has fixed columns; we record the summary in `error` only on
        #  failure; on success we annotate via a dedicated stats JSON in title is overkill,
        #  so we expose counts through get_job's extra fields below.)
        summary = (f"discovered={res.urls_discovered} ingested={res.urls_ingested} "
                   f"failed={res.urls_failed} skipped_robots={res.skipped_robots}")
        await update_job(job_id, total_chunks=res.urls_ingested, done_chunks=res.urls_ingested,
                         error=(summary if res.urls_failed else None))
    except Exception:
        pass


@v1.get("/{tenant}/ingest/sitemap/{job_id}", response_model=SitemapJobOut)
async def get_sitemap_job(tenant: str, job_id: str, auth: TenantDep, request: Request, _: None = Depends(require_secret_key)):
    rate_limit(request, auth.tenant_id)
    j = await job_store.get_job(job_id, auth.tenant_id)
    if j is None:
        raise HTTPException(status_code=404, detail="job not found")
    # The crawl summary is carried in the `error` field on success (see _save_sitemap_stats).
    summary = j.get("error") or ""
    urls_ingested = j.get("total_chunks") or 0
    return SitemapJobOut(
        job_id=job_id, tenant_id=auth.tenant_id, status=j["status"],
        kind="sitemap", progress=j["progress"],
        total_chunks=j.get("total_chunks", 0), done_chunks=j.get("done_chunks", 0),
        urls_ingested=urls_ingested,
        urls_failed=(0 if not summary else 1),
        error=(summary if "failed=" in summary and urls_ingested == 0 else None),
        title=j.get("title"),
    )


# ---------------- PHASE B.3: file upload (PDF / Markdown / code / text) ----------------
from fastapi import File, Form, UploadFile


@v1.post("/{tenant}/documents/upload", response_model=UploadOut, status_code=status.HTTP_201_CREATED)
async def upload_document(
    tenant: str,
    request: Request,
    auth: TenantDep,
    _: None = Depends(require_secret_key),
    file: UploadFile = File(None),
    title: str | None = Form(None),
    content_type: str | None = Form(None),
    metadata: str | None = Form(None),
    acl: str | None = Form(None),
):
    """Upload a file (PDF / Markdown / HTML / code / text) and ingest it.

    multipart/form-data: `file` (required), `title` (optional), `content_type` (optional,
    auto-detected from extension otherwise), `metadata` (optional JSON object string),
    `acl` (optional JSON array of groups). PDFs are parsed with pypdf (clear error if
    missing). Returns the indexed doc_id.
    """
    rate_limit(request, auth.tenant_id)
    from .ingestion.files import detect_content_type, extract_text

    upload = file
    if upload is None:
        raise HTTPException(status_code=422, detail="multipart field 'file' is required")
    fname = getattr(upload, "filename", None) or "upload"
    data = await upload.read()
    if not data:
        raise HTTPException(status_code=422, detail="uploaded file is empty")
    ctype = detect_content_type(fname, content_type)
    try:
        text = extract_text(fname, data, ctype)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    validate_content(text)
    meta = validate_metadata(_parse_json_field(metadata))
    resolved_acl = _resolved_acl(_parse_acl_field(acl), auth, default_to_public=True)
    doc_title = title or fname
    res = await ingest_text(auth.tenant_id, doc_title, text, ctype, meta, acl=resolved_acl)
    INGEST_CHUNKS.inc(res["chunk_count"])
    INGEST_JOBS.labels(status="success").inc()
    await _audit_data_plane(
        "tenant.ingest", auth.tenant_id, target=res["doc_id"],
        meta={"title": doc_title, "chunks": res["chunk_count"],
              "quarantined": res["quarantined_chunks"], "kind": "upload",
              "content_type": ctype},
    )
    return UploadOut(
        doc_id=res["doc_id"], title=doc_title, chunk_count=res["chunk_count"],
        quarantined_chunks=res["quarantined_chunks"], content_type=ctype,
    )


# ---------------- Document management ----------------
@v1.delete("/{tenant}/documents/{doc_id}", status_code=status.HTTP_200_OK)
async def delete_doc(tenant: str, doc_id: str, auth: TenantDep, request: Request, _: None = Depends(require_secret_key)):
    rate_limit(request, auth.tenant_id)
    # Remove the catalog row first so we can read its chunk_count for accurate quota
    # bookkeeping (the previous delete left chunk_count stale — a quota-accuracy bug).
    from .db import delete_document_row, get_document

    row = await get_document(doc_id, auth.tenant_id)
    deleted_rows = await delete_document_row(doc_id, auth.tenant_id)
    delete_document(auth.tenant_id, doc_id)  # drops the Qdrant vectors (tenant-scoped)
    if row:
        # Decrement the tenant chunk counter by the exact number we removed.
        await tenants.increment_chunk_count(auth.tenant_id, -row["chunk_count"])
    await _audit_data_plane("tenant.delete_doc", auth.tenant_id, target=doc_id)
    return {"deleted": doc_id, "catalog_rows": deleted_rows}


@v1.get("/{tenant}/documents", response_model=list[DocumentCatalogOut])
async def list_documents(tenant: str, auth: TenantDep, request: Request, _: None = Depends(require_secret_key), limit: int = 200):
    """PHASE B.4: document catalog — a tenant sees exactly what is indexed for them.

    Tenant-scoped; never cross-tenant (the registry query filters on tenant_id).
    """
    rate_limit(request, auth.tenant_id)
    from .db import list_documents as _list_documents

    docs = await _list_documents(auth.tenant_id, limit=limit)
    return [DocumentCatalogOut(**d) for d in docs]


# ---------------- Query ----------------
@v1.post("/{tenant}/query", response_model=QueryResponse)
async def query(tenant: str, body: QueryRequest, auth: TenantDep, request: Request):
    rate_limit(request, auth.tenant_id)
    s = get_settings()
    t0 = time.perf_counter()

    # (9a) Input-layer guardrail: flag overt injection/jailbreak before anything else.
    injection = bool(s.injection_guard_enabled) and detect_injection(body.question)

    # (9b) Conversational rewrite: resolve follow-ups against session history.
    rewritten = body.question
    was_rewritten = False
    if s.rewrite_enabled and body.session_id:
        rewritten, was_rewritten = rewrite_query(body.session_id, body.question)

    acl_filter = build_acl_filter(_resolved_acl(body.acl, auth, default_to_public=False))
    hits = []
    degraded = False
    try:
        hits = retrieve(
            auth.tenant_id, rewritten, top_k=body.top_k,
            candidate_k=body.candidate_k, rerank=body.rerank, acl_filter=acl_filter,
        )
    except RagError as exc:
        # Backend degraded (Qdrant/embedder/reranker). Return a clean, contract-shaped
        # response with degraded=True rather than a 500 stack trace.
        log.warning("query_degraded", extra={"tenant_id": auth.tenant_id, "error_type": type(exc).__name__})
        degraded = True
    # Filter out any chunks that contain injection
    original_len = len(hits)
    hits = [hit for hit in hits if not detect_injection(hit["text"])]
    filtered_len = len(hits)
    if filtered_len < original_len:
        log.warning(
            "injection_detected_in_retrieved_chunks",
            extra={
                "tenant_id": auth.tenant_id,
                "filtered_count": original_len - filtered_len,
            },
        )
    RETRIEVAL_LATENCY.observe(time.perf_counter() - t0)
    QUERY_HITS.observe(len(hits))

    # (9c) Confidence gating / abstention (Self-RAG style).
    in_scope, _reason = assess_confidence(hits, s.retrieval_confidence_threshold)

    results = [
        RetrievedChunk(
            chunk_id=h["chunk_id"], doc_id=h["doc_id"], title=h["title"],
            text=h["text"],
            score=h.get("rerank_score", h["score"]),
            rerank_score=h.get("rerank_score"),
            metadata=h.get("metadata", {}),
        )
        for h in hits
    ]

    answer = None
    if body.generate:
        if injection:
            # Do not forward a manipulative instruction into the LLM; return a safe refusal.
            answer = ("I can't follow those instructions. Ask me a question about the "
                      "documented content and I'll help.")
        elif not in_scope:
            answer = ("I don't have information on that in the available documents. "
                      "Let me connect you with support, or try rephrasing your question.")
        else:
            answer = generate_answer(rewritten, hits)

    # Best-effort answer text for session history (anchors follow-up rewriting).
    turn_answer = answer or (hits[0]["text"] if hits else "")

    # Record history for the session (only when a session is in use).
    if body.session_id:
        store = get_session_store()
        store.append(body.session_id, "user", body.question)
        store.append(body.session_id, "assistant", turn_answer)

    log.info("query", extra={"tenant_id": auth.tenant_id, "hits": len(hits),
                             "generate": body.generate, "injection": injection,
                             "out_of_scope": (not in_scope), "rewritten": was_rewritten})
    await _audit_data_plane(
        "tenant.query", auth.tenant_id,
        meta={"hits": len(hits), "generate": body.generate, "injection": injection,
              "out_of_scope": (not in_scope), "degraded": degraded},
    )
    return QueryResponse(
        results=results, answer=answer, tenant_id=auth.tenant_id,
        rewritten_query=rewritten if was_rewritten else None,
        out_of_scope=not in_scope, injection_detected=injection,
        degraded=degraded,
    )


@app.post("/api/v1/{tenant}/query/stream")
def query_stream(
    tenant: str,
    body: QueryRequest,
    auth: TenantDep,
    request: Request,
):
    """Server-Sent Events streaming query (v9-3). Emits:
      event: sources  data: <json list of retrieved chunks (titles+snippets)>
      event: token    data: <answer token delta>   (repeated)
      event: done     data: <json {tenant_id, out_of_scope, injection_detected, degraded}>
      event: error    data: <json {error}>           (on failure, instead of done)
    A client can subscribe with EventSource (note: EventSource is GET-only; for POST
    payloads use fetch() + ReadableStream on the client side — see widget.js).
    """
    # tenant identity is derived server-side from the Bearer key (auth.tenant_id);
    # the {tenant} path segment is a URL namespace and is not trusted (matches /query).
    _ = tenant

    # P0 FIX: the SSE endpoint does the same expensive retrieval/rerank/generation as
    # /query, so it MUST enforce the same rate limit. Without this an authenticated
    # tenant could hammer /query/stream with zero quota enforcement.
    rate_limit(request, auth.tenant_id)

    def _sse():
        try:
            rewritten, was_rewritten = rewrite_query(body.session_id, body.question)
            if was_rewritten:
                # Surface the clarified query to the client (useful for chat UIs).
                yield f"event: rewritten\ndata: {json.dumps({'query': rewritten})}\n\n"
            injection = bool(body.question) and detect_injection(body.question)
            acl_filter = build_acl_filter(_resolved_acl(body.acl, auth, default_to_public=False))
            hits = []
            degraded = False
            try:
                hits = retrieve(
                    auth.tenant_id, rewritten, top_k=body.top_k,
                    candidate_k=body.candidate_k, rerank=body.rerank, acl_filter=acl_filter,
                )
            except RagError as exc:
                log.warning("query_stream_degraded", extra={"tenant_id": auth.tenant_id, "error_type": type(exc).__name__})
                degraded = True
            hits = [h for h in hits if not detect_injection(h["text"])]
            in_scope, _ = assess_confidence(hits, get_settings().retrieval_confidence_threshold)

            sources = [{"chunk_id": h["chunk_id"], "title": h.get("title"),
                        "snippet": h["text"][:280]} for h in hits[:body.top_k]]
            yield f"event: sources\ndata: {json.dumps(sources)}\n\n"

            answer = None
            if body.generate:
                if injection:
                    answer = "I can't follow those instructions. Ask me a question about the documented content and I'll help."
                elif not in_scope:
                    answer = "I don't have information on that in the available documents. Let me connect you with support, or try rephrasing your question."
                else:
                    collected: list[str] = []
                    for tok in stream_answer(rewritten, hits):
                        collected.append(tok)
                        yield f"event: token\ndata: {json.dumps(tok)}\n\n"
                    answer = "".join(collected)

            if body.session_id:
                store = get_session_store()
                store.append(body.session_id, "user", body.question)
                store.append(body.session_id, "assistant", answer or (hits[0]["text"] if hits else ""))

            done = {"tenant_id": auth.tenant_id, "out_of_scope": (not in_scope),
                    "injection_detected": injection, "degraded": degraded}
            yield f"event: done\ndata: {json.dumps(done)}\n\n"
        except Exception as exc:
            err = {"error": getattr(exc, "public_detail", type(exc).__name__)}
            yield f"event: error\ndata: {json.dumps(err)}\n\n"

    return StreamingResponse(_sse(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})



@v1.post("/{tenant}/keys", response_model=TenantOut, status_code=status.HTTP_201_CREATED)
async def rotate_api_key(tenant: str, auth: TenantDep, request: Request, _: None = Depends(require_secret_key)):
    """Issue a new API key for this tenant. The old key remains valid until revoked."""
    rate_limit(request, auth.tenant_id)
    new_key = generate_api_key()
    await tenants.add_api_key(auth.tenant_id, new_key)
    await audit_event(
        "key.rotate", actor=auth.tenant_id, target=auth.tenant_id,
        meta={"prefix": new_key[:8]},
    )
    # return only the new key (shown once) alongside tenant info
    return TenantOut(
        tenant_id=auth.tenant_id, name=auth.name, api_key=new_key,
        plan=auth.plan, created_at=auth.created_at, chunk_count=auth.chunk_count,
    )


@v1.get("/{tenant}/keys", response_model=TenantKeysOut)
async def list_keys(tenant: str, auth: TenantDep, request: Request, _: None = Depends(require_secret_key)):
    rate_limit(request, auth.tenant_id)
    keys = [KeyInfo(**k) for k in await tenants.list_key_prefixes(auth.tenant_id)]
    return TenantKeysOut(tenant_id=auth.tenant_id, keys=keys)


@v1.delete("/{tenant}/keys/{prefix}", status_code=status.HTTP_200_OK)
async def revoke_key(tenant: str, prefix: str, auth: TenantDep, request: Request, _: None = Depends(require_secret_key)):
    rate_limit(request, auth.tenant_id)
    n = await tenants.revoke_api_key(auth.tenant_id, prefix)
    if n == 0:
        return {"revoked": 0, "note": "no change (last valid key is protected)"}
    await audit_event(
        "key.revoke", actor=auth.tenant_id, target=auth.tenant_id,
        meta={"prefix": prefix, "revoked": n},
    )
    return {"revoked": n}


@v1.post("/{tenant}/keys/publishable", response_model=TenantOut, status_code=status.HTTP_201_CREATED)
async def create_publishable_key(tenant: str, auth: TenantDep, request: Request, _: None = Depends(require_secret_key)):
    """P1 #9: mint a read-only key (pk_*) safe to embed client-side in the widget.

    The publishable key resolves to the SAME tenant (isolation unchanged) but is
    scope-locked to query endpoints by require_secret_key(); it cannot ingest, delete,
    rotate, or revoke. A tenant may hold any number of publishable keys plus secret keys.
    """
    rate_limit(request, auth.tenant_id)
    new_key = generate_publishable_key()
    await tenants.add_api_key(auth.tenant_id, new_key, kind="publishable")
    await audit_event(
        "key.create_publishable", actor=auth.tenant_id, target=auth.tenant_id,
        meta={"prefix": new_key[:8]},
    )
    return TenantOut(
        tenant_id=auth.tenant_id, name=auth.name, api_key=new_key,
        plan=auth.plan, created_at=auth.created_at, chunk_count=auth.chunk_count,
    )


# ---------------- Evaluation (offline RAG quality, no prod traffic) ----------------
@v1.put("/{tenant}/eval/set", status_code=status.HTTP_200_OK)
async def put_eval_set(tenant: str, body: EvalSetIn, auth: TenantDep, request: Request, _: None = Depends(require_secret_key)):
    rate_limit(request, auth.tenant_id)
    from .eval import save_golden_set
    from .models import GoldenItem

    items = [GoldenItem(**it.model_dump()) for it in body.items]
    n = await save_golden_set(auth.tenant_id, items)
    return {"saved": n}


@v1.post("/{tenant}/eval/run", response_model=EvalReportOut)
async def run_eval(tenant: str, auth: TenantDep, request: Request, _: None = Depends(require_secret_key),
             top_k: int = 8, candidate_k: int = 30, rerank: bool = True):
    rate_limit(request, auth.tenant_id)
    from .eval import evaluate, load_golden_set
    from .models import EvalReportOut as _O

    items = await load_golden_set(auth.tenant_id)
    if not items:
        raise HTTPException(status_code=400, detail="no golden set; PUT /eval/set first")
    rep = evaluate(auth.tenant_id, items, top_k=top_k, candidate_k=candidate_k, rerank=rerank)
    await _audit_data_plane("tenant.eval", auth.tenant_id,
                      meta={"kind": "retrieval", "top_k": top_k, "rerank": rerank})
    return _O(**rep.__dict__)


@v1.post("/{tenant}/eval/quality", response_model=dict)
async def run_eval_quality(tenant: str, auth: TenantDep, request: Request, _: None = Depends(require_secret_key),
                     top_k: int = 8, candidate_k: int = 30, rerank: bool = True,
                     generate_answer: bool = True, persist: bool = True):
    """v9-4: evaluate answer QUALITY (faithfulness + relevancy) via self-hosted LLM judge,
    plus classic retrieval metrics. Persists a run to the trend history when persist=True."""
    rate_limit(request, auth.tenant_id)
    from .eval import evaluate_quality, load_golden_set, save_eval_run

    items = await load_golden_set(auth.tenant_id)
    if not items:
        raise HTTPException(status_code=400, detail="no golden set; PUT /eval/set first")
    rep = evaluate_quality(auth.tenant_id, items, top_k=top_k, candidate_k=candidate_k,
                           rerank=rerank, generate_answer=generate_answer)
    out = {**rep.__dict__}
    if persist:
        rid = await save_eval_run(auth.tenant_id, rep, run_kind="manual_quality")
        out["run_id"] = rid
    await _audit_data_plane("tenant.eval", auth.tenant_id,
                      meta={"kind": "quality", "top_k": top_k, "rerank": rerank,
                            "generate_answer": generate_answer, "persisted": persist})
    return out


@v1.get("/{tenant}/eval/runs", response_model=list[dict])
async def eval_runs(tenant: str, auth: TenantDep, request: Request, _: None = Depends(require_secret_key), limit: int = 50):
    """v9-4: recent eval run history (trend tracking) for this tenant."""
    rate_limit(request, auth.tenant_id)
    from .eval import load_eval_runs

    return await load_eval_runs(auth.tenant_id, limit=limit)


@v1.post("/{tenant}/eval/golden/auto", response_model=dict)
async def auto_golden(tenant: str, body: dict, auth: TenantDep, request: Request, _: None = Depends(require_secret_key),
                      n: int = 3):
    """v9-4: auto-generate golden QA pairs from a provided document via the LLM judge.
    Body: {"title": str, "text": str}. Returns generated EvalItems (and optionally saves)."""
    rate_limit(request, auth.tenant_id)
    from .eval import JudgeLLM, save_golden_set

    title = body.get("title", "")
    text = body.get("text", "")
    if not text:
        raise HTTPException(status_code=400, detail="text required")
    judge = JudgeLLM()
    items = judge.generate_golden(title, text, n=n)
    if body.get("save"):
        await save_golden_set(auth.tenant_id, items)
    return {"generated": [it.__dict__ for it in items], "judge_available": judge.available}


# Mount the versioned API
app.mount("/api/v1", v1)
