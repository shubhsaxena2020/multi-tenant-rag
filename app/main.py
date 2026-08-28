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

import time
import uuid
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, status

from . import jobs as job_store
from . import tenants
from .auth import generate_api_key, get_tenant_from_header, require_admin
from .config import get_settings
from .conversation import (
    assess_confidence,
    detect_injection,
    get_session_store,
    rewrite_query,
)
from .generation import generate_answer
from .ingestion import ingest_text, ingest_url
from .ingestion.runner import submit
from .models import (
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
    TenantCreate,
    TenantKeysOut,
    TenantOut,
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
app = FastAPI(
    title="RAG Service",
    version="1.0.0",
    description="Multi-tenant Retrieval-Augmented Generation service. Tenant API is "
    "versioned under /api/v1. See /api/v1/docs for the interactive contract.",
    openapi_url=None,  # root has no schema; /api/v1 owns the versioned contract
)
app.add_middleware(MetricsMiddleware)

v1 = FastAPI(
    title="RAG Service API",
    version="1.0.0",
    root_path="/api/v1",  # so the generated OpenAPI + Swagger reflect the real mounted URL
    description=(
        "Versioned multi-tenant RAG API. Every tenant route requires "
        "`Authorization: Bearer <tenan...y>`. Tenant identity is resolved "
        "server-side from the key; the {tenant} path segment is informational."
    ),
)


TenantDep = Annotated[tenants.TenantRow, Depends(get_tenant_from_header)]


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


# ---------------- Root (operability) routes ----------------
@app.get("/health")
def health():
    return {"status": "ok", "service": "rag-service", "version": "1.0.0"}


@app.get("/health/ready")
def ready():
    try:
        c = get_client()
        c.get_collections()
        return {"status": "ready", "qdrant": "reachable"}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"qdrant unreachable: {exc}")


@app.get("/metrics")
def metrics():
    body, ctype = metrics_response()
    return body, {"content-type": ctype}


# ---------------- Admin: tenants ---------------
@v1.post("/tenants", response_model=TenantOut, status_code=status.HTTP_201_CREATED)
async def create_tenant(body: TenantCreate, _: None = Depends(require_admin)):
    tenant_id = f"t_{uuid.uuid4().hex[:12]}"
    api_key = generate_api_key()
    row = await tenants.create_tenant(body.name, tenant_id, api_key, body.plan, body.allowed_groups)
    try:
        ensure_collection(get_client())
    except Exception as exc:  # noqa: BLE001 - best-effort; queries create it on demand
        log.warning("ensure_collection failed for %s: %s", tenant_id, exc)
    log.info("tenant_created", extra={"tenant_id": tenant_id, "tenant_name": body.name})
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
async def delete_tenant(tenant_id: str, _: None = Depends(require_admin)):
    """Offboard a tenant: drop its Qdrant collection (hard data removal) and remove
    the registry row. This guarantees no residual vectors remain."""
    dropped = delete_tenant_collection(tenant_id)
    removed = await tenants.delete_tenant(tenant_id)
    if not removed:
        raise HTTPException(status_code=404, detail="tenant not found")
    log.info("tenant_offboarded", extra={"tenant_id": tenant_id, "collection_dropped": dropped})
    return {"deleted": tenant_id, "collection_dropped": dropped}


# ---------------- Synchronous ingestion (convenience, small payloads) ----------------
@v1.post("/{tenant}/documents", response_model=DocumentOut, status_code=status.HTTP_201_CREATED)
async def create_document(tenant: str, body: DocumentCreate, auth: TenantDep, request: Request):
    rate_limit(request, auth.tenant_id)
    validate_content(body.content)
    ct = validate_content_type(body.content_type)
    meta = validate_metadata(body.metadata)
    acl = _resolved_acl(body.acl, auth, default_to_public=True)
    res = await ingest_text(auth.tenant_id, body.title, body.content, ct, meta, acl=acl)
    INGEST_CHUNKS.inc(res["chunk_count"])
    INGEST_JOBS.labels(status="success").inc()
    log.info("document_ingested", extra={"tenant_id": auth.tenant_id, "chunk_count": res["chunk_count"]})
    return DocumentOut(
        doc_id=res["doc_id"], title=res["title"], chunk_count=res["chunk_count"],
        content_type=ct, metadata=meta or {},
    )


@v1.post("/{tenant}/ingest/url", response_model=DocumentOut, status_code=status.HTTP_201_CREATED)
async def ingest_from_url(tenant: str, body: IngestUrl, auth: TenantDep, request: Request):
    rate_limit(request, auth.tenant_id)
    meta = validate_metadata(body.metadata)
    acl = _resolved_acl(body.acl, auth, default_to_public=True)
    res = await ingest_url(auth.tenant_id, body.url, body.title, meta, acl=acl)
    INGEST_CHUNKS.inc(res["chunk_count"])
    INGEST_JOBS.labels(status="success").inc()
    return DocumentOut(
        doc_id=res["doc_id"], title=res["title"], chunk_count=res["chunk_count"],
        content_type="html", metadata=meta or {},
    )


@v1.post("/{tenant}/ingest/text", response_model=DocumentOut, status_code=status.HTTP_201_CREATED)
async def ingest_from_text(tenant: str, body: IngestText, auth: TenantDep, request: Request):
    rate_limit(request, auth.tenant_id)
    validate_content(body.text)
    ct = validate_content_type(body.content_type)
    meta = validate_metadata(body.metadata)
    acl = _resolved_acl(body.acl, auth, default_to_public=True)
    res = await ingest_text(auth.tenant_id, body.title, body.text, ct, meta, acl=acl)
    INGEST_CHUNKS.inc(res["chunk_count"])
    INGEST_JOBS.labels(status="success").inc()
    return DocumentOut(
        doc_id=res["doc_id"], title=res["title"], chunk_count=res["chunk_count"],
        content_type=ct, metadata=meta or {},
    )


# ---------------- Async ingestion jobs (canonical, status-tracked) ----------------
@v1.post("/{tenant}/ingest/jobs", response_model=JobStatus, status_code=status.HTTP_202_ACCEPTED)
async def create_ingest_job(tenant: str, body: IngestJobRequest, auth: TenantDep, request: Request):
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
async def list_ingest_jobs(tenant: str, auth: TenantDep, request: Request, limit: int = 50):
    rate_limit(request, auth.tenant_id)
    return [JobStatus(**j) for j in await job_store.list_jobs(auth.tenant_id, limit)]


@v1.get("/{tenant}/jobs/{job_id}", response_model=JobStatus)
async def get_ingest_job(tenant: str, job_id: str, auth: TenantDep, request: Request):
    rate_limit(request, auth.tenant_id)
    j = await job_store.get_job(job_id, auth.tenant_id)
    if j is None:
        raise HTTPException(status_code=404, detail="job not found")
    return JobStatus(**j)


@v1.delete("/{tenant}/jobs/{job_id}", status_code=status.HTTP_200_OK)
async def delete_ingest_job(tenant: str, job_id: str, auth: TenantDep, request: Request):
    rate_limit(request, auth.tenant_id)
    ok = await job_store.delete_job(job_id, auth.tenant_id)
    if not ok:
        raise HTTPException(status_code=404, detail="job not found")
    return {"deleted": job_id}


# ---------------- Document management ----------------
@v1.delete("/{tenant}/documents/{doc_id}", status_code=status.HTTP_200_OK)
async def delete_doc(tenant: str, doc_id: str, auth: TenantDep, request: Request):
    rate_limit(request, auth.tenant_id)
    delete_document(auth.tenant_id, doc_id)
    return {"deleted": doc_id}


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
    hits = retrieve(
        auth.tenant_id, rewritten, top_k=body.top_k,
        candidate_k=body.candidate_k, rerank=body.rerank, acl_filter=acl_filter,
    )
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
    return QueryResponse(
        results=results, answer=answer, tenant_id=auth.tenant_id,
        rewritten_query=rewritten if was_rewritten else None,
        out_of_scope=not in_scope, injection_detected=injection,
    )


# ---------------- API key management (tenant-scoped rotation) ----------------
@v1.post("/{tenant}/keys", response_model=TenantOut, status_code=status.HTTP_201_CREATED)
async def rotate_api_key(tenant: str, auth: TenantDep, request: Request):
    """Issue a new API key for this tenant. The old key remains valid until revoked."""
    rate_limit(request, auth.tenant_id)
    new_key = generate_api_key()
    await tenants.add_api_key(auth.tenant_id, new_key)
    # return only the new key (shown once) alongside tenant info
    return TenantOut(
        tenant_id=auth.tenant_id, name=auth.name, api_key=new_key,
        plan=auth.plan, created_at=auth.created_at, chunk_count=auth.chunk_count,
    )


@v1.get("/{tenant}/keys", response_model=TenantKeysOut)
async def list_keys(tenant: str, auth: TenantDep, request: Request):
    rate_limit(request, auth.tenant_id)
    keys = [KeyInfo(**k) for k in await tenants.list_key_prefixes(auth.tenant_id)]
    return TenantKeysOut(tenant_id=auth.tenant_id, keys=keys)


@v1.delete("/{tenant}/keys/{prefix}", status_code=status.HTTP_200_OK)
async def revoke_key(tenant: str, prefix: str, auth: TenantDep, request: Request):
    rate_limit(request, auth.tenant_id)
    n = await tenants.revoke_api_key(auth.tenant_id, prefix)
    if n == 0:
        return {"revoked": 0, "note": "no change (last valid key is protected)"}
    return {"revoked": n}


# ---------------- Evaluation (offline RAG quality, no prod traffic) ----------------
@v1.put("/{tenant}/eval/set", status_code=status.HTTP_200_OK)
async def put_eval_set(tenant: str, body: EvalSetIn, auth: TenantDep, request: Request):
    rate_limit(request, auth.tenant_id)
    from .eval import save_golden_set
    from .models import GoldenItem

    items = [GoldenItem(**it.model_dump()) for it in body.items]
    n = await save_golden_set(auth.tenant_id, items)
    return {"saved": n}


@v1.post("/{tenant}/eval/run", response_model=EvalReportOut)
async def run_eval(tenant: str, auth: TenantDep, request: Request,
             top_k: int = 8, candidate_k: int = 30, rerank: bool = True):
    rate_limit(request, auth.tenant_id)
    from .eval import evaluate, load_golden_set
    from .models import EvalReportOut as _O

    items = await load_golden_set(auth.tenant_id)
    if not items:
        raise HTTPException(status_code=400, detail="no golden set; PUT /eval/set first")
    rep = evaluate(auth.tenant_id, items, top_k=top_k, candidate_k=candidate_k, rerank=rerank)
    return _O(**rep.__dict__)


# Mount the versioned API
app.mount("/api/v1", v1)
