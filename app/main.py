"""FastAPI application: multi-tenant RAG service (v1).

Isolation: tenant identity is derived server-side from the Bearer API key on every
request (never from the request body). All vector operations are scoped to the
tenant's own Qdrant collection, so cross-tenant reads are structurally impossible.

Ingestion: synchronous endpoints are convenience for small payloads. The canonical
API for long-running ingestion is the async job flow:
  POST /{tenant}/ingest/jobs  -> {job_id}  (returns immediately)
  GET  /{tenant}/jobs/{id}    -> status/progress/result
  GET  /{tenant}/jobs         -> recent jobs
"""
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, status

from . import jobs as job_store
from . import tenants
from .auth import generate_api_key, get_tenant_from_header, require_admin
from .generation import generate_answer
from .ingestion import ingest_text, ingest_url
from .ingestion.runner import submit
from .models import (
    DocumentCreate,
    DocumentOut,
    IngestJobRequest,
    IngestText,
    IngestUrl,
    JobStatus,
    QueryRequest,
    QueryResponse,
    RetrievedChunk,
    TenantCreate,
    TenantOut,
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

app = FastAPI(title="RAG Service", version="1.0.0")

TenantDep = Annotated[tenants.TenantRow, Depends(get_tenant_from_header)]


@app.get("/health")
def health():
    return {"status": "ok", "service": "rag-service", "version": "1.0.0"}


@app.get("/health/ready")
def ready():
    try:
        c = get_client()
        c.get_collections()
        return {"status": "ready", "qdrant": "reachable"}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"qdrant unreachable: {e}")


@app.post("/tenants", response_model=TenantOut, status_code=status.HTTP_201_CREATED)
def create_tenant(body: TenantCreate, _: None = Depends(require_admin)):
    tenant_id = f"t_{uuid.uuid4().hex[:12]}"
    api_key = generate_api_key()
    row = tenants.create_tenant(body.name, tenant_id, api_key, body.plan)
    try:
        ensure_collection(get_client(), tenant_id)
    except Exception as exc:  # noqa: BLE001 - best-effort; queries create it on demand
        import logging
        logging.getLogger("rag").warning("ensure_collection failed for %s: %s", tenant_id, exc)
    return TenantOut(
        tenant_id=row.tenant_id, name=row.name, api_key=api_key,
        plan=row.plan, created_at=row.created_at,
    )


@app.get("/tenants", response_model=list[TenantOut])
def list_tenants(_: None = Depends(require_admin)):
    return [
        TenantOut(
            tenant_id=t.tenant_id, name=t.name, api_key=f"{t.api_key_prefix}...",
            plan=t.plan, created_at=t.created_at,
        )
        for t in tenants.list_tenants()
    ]


@app.delete("/tenants/{tenant_id}", status_code=status.HTTP_200_OK)
def delete_tenant(tenant_id: str, _: None = Depends(require_admin)):
    """Offboard a tenant: drop its Qdrant collection (hard data removal) and remove
    the registry row. This guarantees no residual vectors remain."""
    dropped = delete_tenant_collection(tenant_id)
    removed = tenants.delete_tenant(tenant_id)
    if not removed:
        raise HTTPException(status_code=404, detail="tenant not found")
    return {"deleted": tenant_id, "collection_dropped": dropped}


# ---------------- Synchronous ingestion (convenience, small payloads) ----------------
@app.post("/{tenant}/documents", response_model=DocumentOut, status_code=status.HTTP_201_CREATED)
def create_document(tenant: str, body: DocumentCreate, auth: TenantDep):
    validate_content(body.content)
    ct = validate_content_type(body.content_type)
    meta = validate_metadata(body.metadata)
    res = ingest_text(auth.tenant_id, body.title, body.content, ct, meta)
    return DocumentOut(
        doc_id=res["doc_id"], title=res["title"], chunk_count=res["chunk_count"],
        content_type=ct, metadata=meta or {},
    )


@app.post("/{tenant}/ingest/url", response_model=DocumentOut, status_code=status.HTTP_201_CREATED)
def ingest_from_url(tenant: str, body: IngestUrl, auth: TenantDep):
    meta = validate_metadata(body.metadata)
    res = ingest_url(auth.tenant_id, body.url, body.title, meta)
    return DocumentOut(
        doc_id=res["doc_id"], title=res["title"], chunk_count=res["chunk_count"],
        content_type="html", metadata=meta or {},
    )


@app.post("/{tenant}/ingest/text", response_model=DocumentOut, status_code=status.HTTP_201_CREATED)
def ingest_from_text(tenant: str, body: IngestText, auth: TenantDep):
    validate_content(body.text)
    ct = validate_content_type(body.content_type)
    meta = validate_metadata(body.metadata)
    res = ingest_text(auth.tenant_id, body.title, body.text, ct, meta)
    return DocumentOut(
        doc_id=res["doc_id"], title=res["title"], chunk_count=res["chunk_count"],
        content_type=ct, metadata=meta or {},
    )


# ---------------- Async ingestion jobs (canonical, status-tracked) ----------------
@app.post("/{tenant}/ingest/jobs", response_model=JobStatus, status_code=status.HTTP_202_ACCEPTED)
def create_ingest_job(tenant: str, body: IngestJobRequest, auth: TenantDep):
    kind = body.kind
    title = body.title
    if kind == "url":
        if not body.url:
            raise HTTPException(422, "url is required for kind=url")
        payload = {"url": body.url, "title": title}
    elif kind == "text":
        if not body.text:
            raise HTTPException(422, "text is required for kind=text")
        validate_content(body.text)
        payload = {"text": body.text, "title": title, "content_type": body.content_type}
    elif kind == "document":
        if not body.content:
            raise HTTPException(422, "content is required for kind=document")
        validate_content(body.content)
        payload = {"content": body.content, "title": title, "content_type": body.content_type}
    else:
        raise HTTPException(422, f"unknown kind: {kind}")
    validate_content_type(body.content_type)
    meta = validate_metadata(body.metadata)
    job_id = job_store.create_job(auth.tenant_id, kind, title or (body.url or "untitled"))
    submit(job_id, auth.tenant_id, kind, payload, meta)
    return JobStatus(
        job_id=job_id, tenant_id=auth.tenant_id, kind=kind, status="pending",
        progress=0.0, total_chunks=0, done_chunks=0, title=title,
    )


@app.get("/{tenant}/jobs", response_model=list[JobStatus])
def list_ingest_jobs(tenant: str, auth: TenantDep, limit: int = 50):
    return [JobStatus(**j) for j in job_store.list_jobs(auth.tenant_id, limit)]


@app.get("/{tenant}/jobs/{job_id}", response_model=JobStatus)
def get_ingest_job(tenant: str, job_id: str, auth: TenantDep):
    j = job_store.get_job(job_id, auth.tenant_id)
    if j is None:
        raise HTTPException(status_code=404, detail="job not found")
    return JobStatus(**j)


@app.delete("/{tenant}/jobs/{job_id}", status_code=status.HTTP_200_OK)
def delete_ingest_job(tenant: str, job_id: str, auth: TenantDep):
    ok = job_store.delete_job(job_id, auth.tenant_id)
    if not ok:
        raise HTTPException(status_code=404, detail="job not found")
    return {"deleted": job_id}


# ---------------- Document management ----------------
@app.delete("/{tenant}/documents/{doc_id}", status_code=status.HTTP_200_OK)
def delete_doc(tenant: str, doc_id: str, auth: TenantDep):
    delete_document(auth.tenant_id, doc_id)
    return {"deleted": doc_id}


# ---------------- Query ----------------
@app.post("/{tenant}/query", response_model=QueryResponse)
def query(tenant: str, body: QueryRequest, auth: TenantDep):
    hits = retrieve(
        auth.tenant_id, body.question, top_k=body.top_k,
        candidate_k=body.candidate_k, rerank=body.rerank,
    )
    results = [
        RetrievedChunk(
            chunk_id=h["chunk_id"], doc_id=h["doc_id"], title=h["title"],
            text=h["text"], score=h["score"], metadata=h.get("metadata", {}),
        )
        for h in hits
    ]
    answer = None
    if body.generate:
        answer = generate_answer(body.question, hits)
    return QueryResponse(results=results, answer=answer, tenant_id=auth.tenant_id)
