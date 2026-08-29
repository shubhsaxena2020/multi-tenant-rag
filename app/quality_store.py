"""PHASE E (#25-#29) — retrieval-quality observability store.

Persists per-query retrieval-quality signals (latency, hit count, faithfulness, rerank delta,
rewrite used, multi-hop used) to a small append-only SQLite table so operators can trend quality
over time and mine regressions. Mirrors the reliable, never-sampled write pattern of usage.py.

This is distinct from the global Prometheus counters (which are not labelled per tenant / per query
and are not persisted). The quality store is the queryable, time-series record used by dashboards
and the nightly eval hook.
"""

from __future__ import annotations

import asyncio
import json

from sqlalchemy import text

from .db import get_session_maker


def record_quality_bg(tenant_id: str, **fields) -> None:
    """Fire-and-forget variant for sync contexts. Best-effort: never raises."""
    import threading

    def _run() -> None:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(record_quality(tenant_id, **fields))
        except Exception:  # pragma: no cover - quality logging must never crash the request
            pass
        finally:
            loop.close()

    threading.Thread(target=_run, daemon=True).start()


async def record_quality(
    tenant_id: str,
    *,
    latency_ms: float | None = None,
    hit_count: int | None = None,
    faithfulness: float | None = None,
    rerank_delta: float | None = None,
    rewrite_used: bool | None = None,
    multi_hop: bool | None = None,
    no_answer: bool | None = None,
) -> None:
    """Append one retrieval-quality event for a tenant. Reliable (one insert), append-only."""
    session_maker = get_session_maker()
    async with session_maker() as session:
        await session.execute(text("""
            CREATE TABLE IF NOT EXISTS query_quality_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT NOT NULL,
                latency_ms REAL,
                hit_count INTEGER,
                faithfulness REAL,
                rerank_delta REAL,
                rewrite_used INTEGER,
                multi_hop INTEGER,
                no_answer INTEGER,
                meta TEXT,
                ts TIMESTAMP WITH TIME ZONE NOT NULL
            )
        """))
        await session.execute(text("""
            INSERT INTO query_quality_events
                (tenant_id, latency_ms, hit_count, faithfulness, rerank_delta,
                 rewrite_used, multi_hop, no_answer, meta, ts)
            VALUES
                (:tenant_id, :latency_ms, :hit_count, :faithfulness, :rerank_delta,
                 :rewrite_used, :multi_hop, :no_answer, :meta, :ts)
        """), {
            "tenant_id": tenant_id,
            "latency_ms": latency_ms,
            "hit_count": hit_count,
            "faithfulness": faithfulness,
            "rerank_delta": rerank_delta,
            "rewrite_used": int(bool(rewrite_used)) if rewrite_used is not None else None,
            "multi_hop": int(bool(multi_hop)) if multi_hop is not None else None,
            "no_answer": int(bool(no_answer)) if no_answer is not None else None,
            "meta": json.dumps({}),
            "ts": "now()",
        })
        await session.commit()
