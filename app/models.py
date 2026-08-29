"""Pydantic request/response schemas for the RAG service v1 API."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ---------- Tenant ----------
class TenantCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    plan: str = "standard"  # standard | enterprise (siloed)
    allowed_groups: list[str] | None = Field(
        default=None,
        description=(
            "Server-side RBAC: sub-user group labels this tenant is provisioned to use. "
            "SECURITY (v9-SEC-D): the default (None/'*') grants NO isolation between "
            "sub-users within the tenant — every caller can request any group label, so "
            "document-level RBAC provides zero intra-tenant separation. To enable real "
            "sub-user isolation, operators MUST pass an explicit group list (e.g. "
            "['support','billing']) and ingest docs with a matching acl. '*' is only safe "
            "for single-user tenants. Operator/admin-set only."
        ),
    )


class TenantOut(BaseModel):
    tenant_id: str
    name: str
    api_key: str
    plan: str
    created_at: datetime
    chunk_count: int = 0
    allowed_groups: list[str] = ["*"]


# Alias for backward compatibility with db layer
TenantRow = TenantOut


class KeyInfo(BaseModel):
    prefix: str
    created_at: str
    revoked: bool
    kind: str = "secret"  # "secret" (rk_*) or "publishable" (pk_*) — P1 #9
    expires_at: str | None = None  # v10.8: ISO expiry, or null = never


class KeyExpiryRequest(BaseModel):
    """v10.8: set/clear a key's expiry by prefix. None = clear (never expires)."""
    expires_at: str | None = Field(
        default=None,
        description="UTC ISO-8601 expiry (e.g. 2026-12-31T23:59:59Z), or null to clear.",
    )


class PublishableKeyRequest(BaseModel):
    """P1 #9 + v10.8: mint a read-only publishable key, optionally time-boxed."""
    expires_at: str | None = Field(
        default=None,
        description="Optional UTC ISO-8601 expiry for the publishable key.",
    )


class SecretKeyRequest(BaseModel):
    """v10.8: mint an additional secret key, optionally time-boxed."""
    expires_at: str | None = Field(
        default=None,
        description="Optional UTC ISO-8601 expiry for the new secret key.",
    )


class TenantKeysOut(BaseModel):
    tenant_id: str
    keys: list[KeyInfo]


# ---------- Ingestion ----------
class DocumentCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=300)
    content: str = Field(..., min_length=1)
    content_type: str = "text"  # text | markdown | html | code
    metadata: dict[str, Any] = Field(default_factory=dict)
    acl: list[str] | None = Field(
        default=None,
        description="Optional sub-user group ids allowed to see this doc (document-level RBAC).",
    )


class IngestUrl(BaseModel):
    url: str = Field(..., min_length=1)
    title: str | None = None
    content_type: str = "html"
    metadata: dict[str, Any] = Field(default_factory=dict)
    acl: list[str] | None = None


class IngestText(BaseModel):
    title: str = Field(..., min_length=1, max_length=300)
    text: str = Field(..., min_length=1)
    content_type: str = "text"
    metadata: dict[str, Any] = Field(default_factory=dict)
    acl: list[str] | None = None


class DocumentOut(BaseModel):
    doc_id: str
    title: str
    chunk_count: int
    quarantined_chunks: int = 0  # ingest-time injection drops (poisoned chunks never indexed)
    content_type: str
    metadata: dict[str, Any]
    content_hash: str | None = Field(
        default=None,
        description="sha256 of the cleaned indexed content (GitHub issue #4). Two ingests of "
        "identical content share a hash, enabling idempotent re-ingestion.",
    )
    previous_doc_id: str | None = Field(
        default=None,
        description="When this ingest replaced a prior version of the same source, the doc_id "
        "that was superseded (and whose chunks were deleted). Absent on first ingest / identical re-ingest.",
    )


# ---------- Sitemap onboarding (issue #7) ----------
class DocumentCatalogOut(BaseModel):
    """Catalog row: what a tenant has indexed (issue #7). Backed by document_registry."""

    doc_id: str
    doc_key: str
    title: str
    content_type: str
    chunk_count: int
    source_url: str | None = None
    content_hash: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class SitemapIngestIn(BaseModel):
    url: str = Field(..., min_length=1)
    max_urls: int = Field(default=100, ge=1, le=2000)
    concurrency: int = Field(default=4, ge=1, le=8)
    title: str | None = None
    content_type: str = "html"
    metadata: dict[str, Any] = Field(default_factory=dict)
    acl: list[str] | None = None


class SitemapJobOut(BaseModel):
    job_id: str
    tenant_id: str
    status: str
    kind: str = "sitemap"
    progress: float = 0.0
    total_chunks: int = 0
    done_chunks: int = 0
    urls_discovered: int = 0
    urls_ingested: int = 0
    urls_failed: int = 0
    skipped_robots: int = 0
    error: str | None = None
    title: str | None = None


# ---------- Ingestion jobs ----------
class IngestJobRequest(BaseModel):
    kind: str = Field(default="text", pattern="^(text|url|document)$")
    title: str | None = Field(default=None, max_length=300)
    text: str | None = None
    content: str | None = None
    url: str | None = None
    content_type: str = "text"
    metadata: dict[str, Any] = Field(default_factory=dict)
    acl: list[str] | None = None


class JobStatus(BaseModel):
    job_id: str
    tenant_id: str
    kind: str
    status: str
    progress: float
    total_chunks: int
    done_chunks: int
    error: str | None = None
    result_doc_id: str | None = None
    title: str | None = None


# ---------- Query ----------
class QueryRequest(BaseModel):
    question: str = Field(..., min_length=1)
    top_k: int = Field(default=8, ge=1, le=50)
    candidate_k: int = Field(default=30, ge=1, le=200)
    rerank: bool = True
    generate: bool = False
    acl: list[str] | None = Field(
        default=None,
        description="Sub-user group ids of the caller; restricts retrieval to chunks "
        "whose acl intersects these groups (document-level RBAC). Server-validated against "
        "the tenant's provisioned allowed_groups.",
    )
    session_id: str | None = Field(
        default=None,
        description="Conversation session id. When set, the question is rewritten against "
        "prior turns (follow-up resolution) and history is recorded. Enables a chat widget.",
    )


class RetrievedChunk(BaseModel):
    chunk_id: str
    doc_id: str
    title: str
    text: str
    score: float
    metadata: dict[str, Any] = Field(default_factory=dict)
    rerank_score: float | None = Field(
        default=None,
        description="Post-rerank cross-encoder score, when reranking is enabled. `score` "
        "always reflects the ordering shown to the client (rerank_score when available, "
        "else the fusion score), so consumers must not re-sort by `score`.",
    )


class QueryResponse(BaseModel):
    results: list[RetrievedChunk]
    answer: str | None = None
    tenant_id: str
    rewritten_query: str | None = Field(
        default=None,
        description="The self-contained query actually used for retrieval (when conversational "
        "rewriting was applied). Useful for transparency/debugging.",
    )
    out_of_scope: bool = Field(
        default=False,
        description="True when retrieval confidence was below threshold (or no context found): "
        "the answer is a graceful 'no information' handoff, not a grounded answer.",
    )
    injection_detected: bool = Field(
        default=False,
        description="True when the user turn matched an injection/jailbreak guardrail pattern.",
    )
    degraded: bool = Field(
        default=False,
        description="True when a backend dependency (Qdrant/embedder/reranker) was degraded or "
        "unavailable and the service returned a best-effort response instead of failing.",
    )


# ---------- Eval ----------
class GoldenItem(BaseModel):
    question: str
    relevant_doc_ids: list[str] = Field(default_factory=list)
    relevant_texts: list[str] = Field(default_factory=list)
    expected_answer: str = ""


class EvalSetIn(BaseModel):
    items: list[GoldenItem]


class EvalReportOut(BaseModel):
    questions: int
    hit_rate: float
    mrr: float
    ndcg: float
    context_recall: float
    avg_latency_ms: float
