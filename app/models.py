"""Pydantic request/response schemas for the RAG service v1 API."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field
from .rbac import PUBLIC_GROUP


# ---------- Tenant ----------
class TenantCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    plan: str = "standard"  # standard | enterprise (siloed)
    allowed_groups: list[str] = Field(
        default_factory=lambda: [PUBLIC_GROUP],
        description=(
            "Server-side RBAC: sub-user group labels this tenant is provisioned to use. "
            f"Defaults to [{PUBLIC_GROUP!r}] so documents tagged for other groups are not "
            "returned when a caller omits acl. Operators may explicitly set ['*'] for "
            "backward-compatible unrestricted tenant access, or provision named groups."
        ),
    )
    branding: dict | None = Field(
        default=None,
        description=(
            "Per-tenant widget branding (issue #23): {logo_url, header_title, accent, "
            "accent_text, font_family}. Applied by the embeddable widget via CSS custom "
            "properties. Server-side validated/sanitized before use."
        ),
    )
    system_prompt: str | None = Field(
        default=None,
        description=(
            "PHASE D (#35): per-tenant custom system prompt / persona (operator-trusted "
            "config, NOT end-user input). Prepended as a real `system` message to generation; "
            "empty/None = default generic grounding prompt."
        ),
    )




class TenantAllowedGroupsIn(BaseModel):
    """Operator-set document-RBAC groups for an existing tenant."""
    allowed_groups: list[str] = Field(..., min_length=1)

class TenantOut(BaseModel):
    tenant_id: str
    name: str
    api_key: str | None = None
    plan: str
    created_at: datetime
    chunk_count: int = 0
    allowed_groups: list[str] = [PUBLIC_GROUP]
    branding: dict = {}
    system_prompt: str = ""
    lead_webhook_url: str | None = None
    ingest_webhook_url: str | None = None
    # PHASE I: per-tenant rate limit overrides
    rate_limit_rpm: int | None = None
    ingest_rate_limit_rpm: int | None = None
    chunk_quota: int | None = None


class TenantSystemPromptIn(BaseModel):
    """PHASE D (#35): operator-set persona. Admin-only. It is OPERATOR-trusted config (not
    end-user input), so it is NOT subject to the user-input injection filtering owned by the
    parallel P0/P1 security session. Max length keeps the persisted blob bounded."""
    system_prompt: str = Field(..., max_length=8000)


class TenantWebhooksIn(BaseModel):
    """Tenant-configurable outbound webhook URLs (lead + ingestion callbacks)."""
    lead_webhook_url: str | None = Field(default=None, max_length=2000)
    ingest_webhook_url: str | None = Field(default=None, max_length=2000)


class TenantBranding(BaseModel):
    """Per-tenant widget branding (issue #23). All fields optional; validated/sanitized
    server-side before being returned to the widget."""
    logo_url: str | None = Field(default=None, description="http(s) URL to a logo image.")
    header_title: str | None = Field(default=None, max_length=120, description="Header text.")
    accent: str | None = Field(
        default=None,
        description="Primary accent color as a safe CSS color (#rgb(a) or rgb()/hsl()).",
    )
    accent_text: str | None = Field(
        default=None, description="Text/icon color on the accent background (safe CSS color)."
    )
    font_family: str | None = Field(
        default=None, max_length=120, description="CSS font-family stack (letters, spaces, commas, dashes only)."
    )


class WidgetConfigOut(BaseModel):
    """Branding payload the embeddable widget fetches to self-theme (issue #23)."""
    tenant_id: str
    branding: dict


# Alias for backward compatibility with db layer
TenantRow = TenantOut


def sanitize_branding(raw: dict | None) -> dict:
    """Validate and whitelist tenant branding fields (issue #23).

    Server-side mirror of the widget's applyBranding guards. Rejects anything that could
    enable CSS/HTML injection: accent/accent_text must be a safe CSS color, logo_url must be
    an http(s) URL, font_family only allows letters/spaces/commas/dashes, header_title is a
    short string. Unknown keys are dropped. Returns a clean dict safe to ship to the widget.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    # Colors
    import re

    color_re = re.compile(r"^(#[0-9a-fA-F]{3,8}|rgb\(\s*\d{1,3}%?\s*,\s*\d{1,3}%?\s*,\s*\d{1,3}%?\s*\)|hsl\(\s*\d{1,3}\s*,\s*\d{1,3}%\s*,\s*\d{1,3}%\s*\))$")
    for key in ("accent", "accent_text"):
        v = raw.get(key)
        if isinstance(v, str) and color_re.match(v.strip()):
            out[key] = v.strip()
    # Logo URL — http(s) only
    logo = raw.get("logo_url")
    if isinstance(logo, str):
        try:
            from urllib.parse import urlparse

            p = urlparse(logo)
            if p.scheme in ("http", "https") and p.netloc:
                out["logo_url"] = logo
        except Exception:
            pass
    # Header title — plain text only (no HTML). Reject anything with markup characters.
    title = raw.get("header_title")
    if isinstance(title, str) and 0 < len(title) <= 120 and not re.search(r"[<>]", title):
        out["header_title"] = title
    # Font family — letters, spaces, commas, dashes, quotes only
    font = raw.get("font_family")
    if isinstance(font, str) and re.match(r"^[A-Za-z0-9 ,'\-\"]{1,120}$", font):
        out["font_family"] = font
    return out


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


class DocumentChunkOut(BaseModel):
    """A single chunk of a document, as returned by the source viewer (issue #6)."""
    chunk_id: str
    title: str | None = None
    text: str
    metadata: dict[str, Any] = {}


class DocumentChunksOut(BaseModel):
    """Full document content for the hosted source viewer / citation deep-links (issue #6)."""
    doc_id: str
    title: str
    content_type: str
    source_url: str | None = None
    chunks: list[DocumentChunkOut]


class DocumentCatalogItem(BaseModel):
    """One row in a tenant's index catalog (issue #12).

    Exposes only tenant-relevant fields. The internal `doc_key` (registry stability key)
    is intentionally NOT surfaced.
    """

    doc_id: str
    title: str
    content_type: str
    chunk_count: int
    source_url: str | None = None
    content_hash: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class DocumentCatalogPage(BaseModel):
    """Paginated catalog response (issue #12): items + true total + paging cursor."""

    items: list[DocumentCatalogItem]
    total: int
    limit: int
    offset: int


class UploadOut(BaseModel):
    doc_id: str
    title: str
    chunk_count: int
    quarantined_chunks: int = 0
    content_type: str


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
    rewrite: bool = Field(
        default=True,
        description="Pre-retrieval query rewriting (PHASE B): clarify/expand short queries and "
        "decompose multi-part questions before the vector search. Set false to skip (use the raw "
        "question verbatim). Passthrough when no LLM is configured.",
    )
    hops: int | None = Field(
        default=None,
        ge=1, le=5,
        description="Multi-hop retrieval depth (PHASE C). 1 = single retrieval (default). >1 "
        "requires a plan that permits multi-hop (enterprise/pro); standard tenants requesting >1 "
        "receive 402. Capped at 5.",
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
        description="The self-contained query actually used for retrieval (pre-retrieval and/or "
        "conversational rewriting applied). Useful for transparency/debugging.",
    )
    sub_questions: list[str] | None = Field(
        default=None,
        description="Sub-questions the query was decomposed into when it was multi-part/comparative "
        "(PHASE B). Empty list when single-part. Surfaced for transparency/observability.",
    )
    hop_count: int | None = Field(
        default=None,
        description="Number of retrieval hops actually executed (PHASE C). 1 for single retrieval; "
        ">1 when multi-hop was used. None when not applicable.",
    )
    faithfulness: float | None = Field(
        default=None,
        ge=0.0, le=1.0,
        description="Estimated grounding score of the generated answer vs retrieved context "
        "(PHASE D), 0..1. Higher = more faithful. None when no answer was generated.",
    )
    answerable: bool | None = Field(
        default=None,
        description="Whether the answer is grounded in / supported by the retrieved context "
        "(PHASE D). False for a safe 'I don't know' refusal. None when no answer was generated.",
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


# ---------- Feedback (PHASE D: thumbs up/down capture) ----------
from typing import Literal

class FeedbackIn(BaseModel):
    rating: Literal["up", "down"]
    session_id: str | None = None
    message_id: str | None = None
    comment: str | None = Field(default=None, max_length=2000)
    question: str | None = None
    answer: str | None = None


# ---------- Handoff / lead capture (PHASE D: out-of-scope queries) ----------
class HandoffIn(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    name: str | None = Field(default=None, max_length=200)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=50)
    message: str | None = Field(default=None, max_length=4000)
    session_id: str | None = None
