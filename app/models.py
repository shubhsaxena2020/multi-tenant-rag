"""Pydantic request/response schemas for the RAG service v1 API."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ---------- Tenant ----------
class TenantCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    plan: str = "standard"  # standard | enterprise (siloed)


class TenantOut(BaseModel):
    tenant_id: str
    name: str
    api_key: str
    plan: str
    created_at: datetime
    chunk_count: int = 0


class KeyInfo(BaseModel):
    prefix: str
    created_at: str
    revoked: bool


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
    content_type: str
    metadata: dict[str, Any]


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
    candidate_k: int = Field(default=100, ge=1, le=200)
    rerank: bool = True
    generate: bool = False
    acl: list[str] | None = Field(
        default=None,
        description="Sub-user group ids of the caller; restricts retrieval to chunks "
        "whose acl intersects these groups (document-level RBAC).",
    )


class RetrievedChunk(BaseModel):
    chunk_id: str
    doc_id: str
    title: str
    text: str
    score: float
    metadata: dict[str, Any] = Field(default_factory=dict)


class QueryResponse(BaseModel):
    results: list[RetrievedChunk]
    answer: str | None = None
    tenant_id: str


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
