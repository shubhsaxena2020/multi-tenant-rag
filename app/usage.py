"""Per-tenant usage metering & reporting (PHASE D: business value, issue #27).

A reliable, queryable record of billable/operational events per tenant — distinct from the
*hashed* audit trail (sampled, best-effort, for accountability) and from the *global*
Prometheus counters (not labelled per tenant). Every query, document ingestion, chunk
ingested, and eval run is recorded here so operators can report real business value
(usage volume, growth, overage signals) per tenant and over time.

Events are written synchronously and reliably (not sampled) so counts are exact. The table
is append-only and tiny per row.

Event kinds:
  - "query"            : one RAG query (POST /query or /query/stream)
  - "doc_ingested"     : one document ingested (text/url/upload/sitemap item)
  - "chunk_ingested"   : N chunks created (count column carries the number)
  - "eval_run"         : one retrieval or quality eval run
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from .db import get_session_maker


def record_usage_bg(tenant_id: str, kind: str, count: int = 1, meta: dict | None = None) -> None:
    """Fire-and-forget variant for sync contexts (e.g. the SSE endpoint, which runs in a
    worker thread without a running event loop). Spawns a short-lived thread that drives the
    async insert on its own loop. Best-effort: exceptions are swallowed (metering must never
    break the request)."""
    import threading

    def _run() -> None:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(record_usage(tenant_id, kind, count=count, meta=meta))
        except Exception:  # pragma: no cover - metering must never crash the request
            pass
        finally:
            loop.close()

    threading.Thread(target=_run, daemon=True).start()


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def record_usage(tenant_id: str, kind: str, count: int = 1, meta: dict | None = None) -> None:
    """Append a usage event for a tenant. Reliable (one insert), never sampled."""
    if count <= 0:
        return
    session_maker = get_session_maker()
    async with session_maker() as session:
        await session.execute(text("""
            CREATE TABLE IF NOT EXISTS usage_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 1,
                meta TEXT,
                ts TIMESTAMP WITH TIME ZONE NOT NULL
            )
        """))
        await session.execute(text("""
            INSERT INTO usage_events (tenant_id, kind, count, meta, ts)
            VALUES (:tenant_id, :kind, :count, :meta, :ts)
        """), {
            "tenant_id": tenant_id,
            "kind": kind,
            "count": count,
            "meta": json.dumps(meta or {}),
            "ts": _now(),
        })
        await session.commit()


@dataclass
class UsageSummary:
    tenant_id: str
    period_start: datetime
    period_end: datetime
    queries: int = 0
    docs_ingested: int = 0
    chunks_ingested: int = 0
    eval_runs: int = 0
    # raw kind->count map for forward-compat / debugging
    by_kind: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "tenant_id": self.tenant_id,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "queries": self.queries,
            "docs_ingested": self.docs_ingested,
            "chunks_ingested": self.chunks_ingested,
            "eval_runs": self.eval_runs,
            "by_kind": self.by_kind,
        }


async def get_usage_summary(tenant_id: str, *, days: int = 30) -> UsageSummary:
    """Aggregate usage for a tenant over the last `days` days (inclusive of today)."""
    end = _now()
    start = end - timedelta(days=days)
    session_maker = get_session_maker()
    async with session_maker() as session:
        rows = (await session.execute(text("""
            SELECT kind, COALESCE(SUM(count), 0) AS total
            FROM usage_events
            WHERE tenant_id = :tenant_id AND ts >= :start
            GROUP BY kind
        """), {"tenant_id": tenant_id, "start": start})).fetchall()
    totals: dict[str, int] = {r[0]: int(r[1]) for r in rows}
    return UsageSummary(
        tenant_id=tenant_id,
        period_start=start,
        period_end=end,
        queries=totals.get("query", 0),
        docs_ingested=totals.get("doc_ingested", 0),
        chunks_ingested=totals.get("chunk_ingested", 0),
        eval_runs=totals.get("eval_run", 0),
        by_kind=totals,
    )


async def get_usage_timeseries(tenant_id: str, *, days: int = 30, kind: str | None = None) -> list[dict]:
    """Daily rollup per tenant for charts/exports. Returns one row per day with kind counts.

    Each item: {date: 'YYYY-MM-DD', queries, docs_ingested, chunks_ingested, eval_runs, total}.
    """
    end = _now()
    start = end - timedelta(days=days)
    session_maker = get_session_maker()
    async with session_maker() as session:
        sql = """
            SELECT DATE(ts) AS day, kind, COALESCE(SUM(count), 0) AS total
            FROM usage_events
            WHERE tenant_id = :tenant_id AND ts >= :start
        """
        params: dict = {"tenant_id": tenant_id, "start": start}
        if kind:
            sql += " AND kind = :kind"
            params["kind"] = kind
        sql += " GROUP BY DATE(ts), kind ORDER BY day ASC"
        rows = (await session.execute(text(sql), params)).fetchall()

    by_day: dict[str, dict] = {}
    for r in rows:
        day = str(r[0])
        k = r[1]
        total = int(r[2])
        bucket = by_day.setdefault(day, {
            "date": day, "queries": 0, "docs_ingested": 0,
            "chunks_ingested": 0, "eval_runs": 0, "total": 0,
        })
        if k == "query":
            bucket["queries"] += total
        elif k == "doc_ingested":
            bucket["docs_ingested"] += total
        elif k == "chunk_ingested":
            bucket["chunks_ingested"] += total
        elif k == "eval_run":
            bucket["eval_runs"] += total
        bucket["total"] += total
    return list(by_day.values())
