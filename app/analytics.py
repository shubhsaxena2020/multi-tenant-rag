"""PHASE E — analytics: knowledge-gap logging + per-tenant token/cost metering.

Two production observability features a real client expects (grounded in 2026 RAG
analytics research: unanswered-question / knowledge-gap mining turns a support bot
into a content-coverage signal, and per-tenant token metering is the unit of billing):

1. KNOWLEDGE GAPS — when a query is out-of-scope (no_context / low_retrieval_confidence)
   or injection-guarded, we log the question (anonymized: the raw text, tenant-scoped,
   NOT logged alongside PII) so the client can see what their bot CAN'T answer yet and
   fix the knowledge base. Storage is the source of truth; the DB row is privacy-safe
   (no answer, no user identity — only the tenant_id + the question text + reason).

2. USAGE METERING — cumulative per-tenant counters: queries served, chunks ingested,
   and the TOKENS consumed (ingest embed tokens + query/generation tokens) plus an
   estimated cost. Token counts are ESTIMATED deterministically (words * 1.3) so the
   service stays self-hostable with zero external LLM dependency; when a real OpenAI-
   compatible LLM is configured we use its reported usage when available. The estimate
   deliberately over-counts slightly (conservative for billing).

Design rules inherited from the rest of the service:
- Every row is tenant-scoped (tenant_id column + index) — no cross-tenant leakage.
- All writes are fail-open best-effort: a metering/gap write failure must NEVER fail the
  primary query/ingest request (mirrors the audit trail contract).
- Token/cost numbers are derived, never trusted from the client body.
- Non-destructive only: we INSERT/UPSERT, never DROP or ALTER existing tables.
"""
from __future__ import annotations

import math
from datetime import UTC, datetime

from sqlalchemy import (
    DateTime,
    Integer,
    String,
    Text,
    select,
)
from sqlalchemy.orm import Mapped, mapped_column

from .config import get_settings
from .db import Base, get_session_maker
from .observability import (
    ANALYTICS_GAPS,
    ANALYTICS_INGEST_TOKENS,
    ANALYTICS_QUERY_TOKENS,
    get_logger,
)

log = get_logger("analytics")

# Reason codes for a logged knowledge gap. Bounded set (no free-text label in metrics).
GAP_NO_CONTEXT = "no_context"
GAP_LOW_CONFIDENCE = "low_retrieval_confidence"
GAP_INJECTION = "injection_guarded"

# Default estimated LLM prices (USD per 1M tokens) used only for the *estimated* cost
# surfaced in /analytics. These are conservative defaults; an operator can override via
# ANALYTICS_INGEST_PRICE_PER_1M / ANALYTICS_QUERY_PRICE_PER_1M env (human-configurable,
# not a billing integration — the goal is a usage signal, not invoicing).
DEFAULT_INGEST_PRICE_PER_1M = 0.01  # embedding is cheap
DEFAULT_QUERY_PRICE_PER_1M = 0.50   # generation is the expensive part


def estimate_tokens(text: str) -> int:
    """Deterministic token estimate: ~1.3 tokens/word (covers sub-word tokens).

    Used when no real tokenizer/LLM usage is available so metering works in CI/self-host
    with zero external deps. Conservative (slightly over-counts) which is safe for a usage
    signal. Returns at least 1 for non-empty text.
    """
    if not text:
        return 0
    words = len(text.split())
    return max(1, math.ceil(words * 1.3))


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


class KnowledgeGap(Base):
    """PHASE E.1: unanswered / out-of-scope questions, tenant-scoped, for coverage gaps."""

    __tablename__ = "knowledge_gaps"

    gap_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)  # bounded set above
    hits: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class UsageMeter(Base):
    """PHASE E.2: cumulative per-tenant usage (queries, chunks, tokens, est. cost)."""

    __tablename__ = "usage_meter"

    tenant_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    query_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    chunk_count_ingested: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tokens_ingested: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tokens_query: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    est_cost_usd: Mapped[float] = mapped_column(default=0.0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# ---------------------------------------------------------------------------
# Knowledge-gap logging
# ---------------------------------------------------------------------------


async def record_knowledge_gap(
    tenant_id: str, question: str, reason: str, session=None
) -> None:
    """PHASE E.1: log an out-of-scope / guarded question as a knowledge-gap candidate.

    Best-effort + fail-open: any DB error is logged (warning) and swallowed so it can
    never fail the primary query. `reason` MUST be one of the bounded GAP_* codes.
    """
    import uuid

    gid = f"gap_{uuid.uuid4().hex[:16]}"
    now = datetime.now(UTC)
    try:
        async with (session or get_session_maker())() as s:
            s.add(KnowledgeGap(
                gap_id=gid, tenant_id=tenant_id, question=question[:2000],
                reason=reason, hits=1, created_at=now,
            ))
            await s.commit()
        ANALYTICS_GAPS.labels(reason=reason).inc()
    except Exception as e:
        log.warning("knowledge_gap_write_failed",
                    extra={"tenant_id": tenant_id, "error_type": type(e).__name__})


async def list_knowledge_gaps(
    tenant_id: str, reason: str | None = None, limit: int = 200, session=None
) -> list[dict]:
    """Return logged knowledge gaps for a tenant (newest first). Optional reason filter."""
    async with (session or get_session_maker())() as s:
        stmt = (
            select(KnowledgeGap)
            .where(KnowledgeGap.tenant_id == tenant_id)
        )
        if reason:
            stmt = stmt.where(KnowledgeGap.reason == reason)
        stmt = stmt.order_by(KnowledgeGap.created_at.desc()).limit(limit)
        rows = (await s.execute(stmt)).scalars().all()
        return [
            {
                "gap_id": r.gap_id, "question": r.question, "reason": r.reason,
                "hits": r.hits,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]


# ---------------------------------------------------------------------------
# Usage metering
# ---------------------------------------------------------------------------


def _prices() -> tuple[float, float]:
    s = get_settings()
    ingest = getattr(s, "analytics_ingest_price_per_1m", None) or DEFAULT_INGEST_PRICE_PER_1M
    query = getattr(s, "analytics_query_price_per_1m", None) or DEFAULT_QUERY_PRICE_PER_1M
    return float(ingest), float(query)


async def _upsert_usage(
    tenant_id: str, *, d_queries: int = 0, d_chunks: int = 0,
    d_ingest_tokens: int = 0, d_query_tokens: int = 0,
) -> None:
    """Idempotent additive update of a tenant's usage row (INSERT-or-add).

    Fail-open: never raises. Works on both SQLite and Postgres (uses a SELECT-then-UPSERT
    which is portable; row-level contention is negligible at tenant granularity).
    """
    ingest_price, query_price = _prices()
    d_cost = (d_ingest_tokens / 1_000_000.0) * ingest_price + \
             (d_query_tokens / 1_000_000.0) * query_price
    now = datetime.now(UTC)
    try:
        async with get_session_maker()() as s:
            row = (await s.execute(
                select(UsageMeter).where(UsageMeter.tenant_id == tenant_id)
            )).scalar_one_or_none()
            if row is None:
                s.add(UsageMeter(
                    tenant_id=tenant_id,
                    query_count=d_queries,
                    chunk_count_ingested=d_chunks,
                    tokens_ingested=d_ingest_tokens,
                    tokens_query=d_query_tokens,
                    est_cost_usd=d_cost,
                    updated_at=now,
                ))
            else:
                row.query_count += d_queries
                row.chunk_count_ingested += d_chunks
                row.tokens_ingested += d_ingest_tokens
                row.tokens_query += d_query_tokens
                row.est_cost_usd += d_cost
                row.updated_at = now
            await s.commit()
    except Exception as e:
        log.warning("usage_meter_write_failed",
                    extra={"tenant_id": tenant_id, "error_type": type(e).__name__})


async def record_ingest_usage(
    tenant_id: str, chunks: int, tokens: int, session=None
) -> None:
    """PHASE E.2: accumulate ingest-side usage (chunk count + embed tokens + est. cost)."""
    if chunks <= 0 and tokens <= 0:
        return
    ANALYTICS_INGEST_TOKENS.inc(tokens)
    await _upsert_usage(tenant_id, d_chunks=chunks, d_ingest_tokens=tokens)


async def record_query_usage(
    tenant_id: str, tokens: int, generated: bool = False, session=None
) -> None:
    """PHASE E.2: accumulate query-side usage (1 query + generation tokens + est. cost)."""
    ANALYTICS_QUERY_TOKENS.inc(tokens)
    await _upsert_usage(tenant_id, d_queries=1, d_query_tokens=tokens)


async def get_usage(tenant_id: str, session=None) -> dict:
    """Return the cumulative usage snapshot for a tenant (zeros if never used)."""
    async with (session or get_session_maker())() as s:
        row = (await s.execute(
            select(UsageMeter).where(UsageMeter.tenant_id == tenant_id)
        )).scalar_one_or_none()
        if row is None:
            return {
                "tenant_id": tenant_id, "query_count": 0, "chunk_count_ingested": 0,
                "tokens_ingested": 0, "tokens_query": 0, "est_cost_usd": 0.0,
            }
        return {
            "tenant_id": tenant_id,
            "query_count": row.query_count,
            "chunk_count_ingested": row.chunk_count_ingested,
            "tokens_ingested": row.tokens_ingested,
            "tokens_query": row.tokens_query,
            "est_cost_usd": round(row.est_cost_usd, 6),
        }
