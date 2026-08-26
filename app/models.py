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


# ---------- Ingestion ----------
class DocumentCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=300)
    content: str = Field(..., min_length=1)
    content_type: str = "text"  # text | markdown | html | code
    metadata: dict[str, Any] = Field(default_factory=dict)


class IngestUrl(BaseModel):
    url: str = Field(..., min_length=1)
    title: str | None = None
    content_type: str = "html"
    metadata: dict[str, Any] = Field(default_factory=dict)


class IngestText(BaseModel):
    title: str = Field(..., min_length=1, max_length=300)
    text: str = Field(..., min_length=1)
    content_type: str = "text"
    metadata: dict[str, Any] = Field(default_factory=dict)


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
    text: str | None = None          # for kind=text
    content: str | None = None       # for kind=document
    url: str | None = None           # for kind=url
    content_type: str = "text"
    metadata: dict[str, Any] = Field(default_factory=dict)


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
    candidate_k: int = Field(default=50, ge=1, le=200)  # initial vector recall
    rerank: bool = True
    generate: bool = False  # optional answer generation (pluggable provider)


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
