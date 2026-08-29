"""PHASE E — unified per-tenant analytics aggregate (issue #37).

Combines the raw event sources accumulated in Phase D into operator-usable insight:
  - usage      (app/usage.py):  query/doc/chunk/eval volume + daily timeseries
  - feedback   (app/feedback.py): thumbs up/down + captured questions
  - leads      (app/leads.py):   human-handoff captures for out-of-scope queries
  - usage meta now carries out_of_scope (persisted on each /query in Phase E) so we can
    report an out-of-scope rate.

The aggregate is a single read over the three stores — no new storage. `top_questions` is
derived honestly from the only places user question text is captured: feedback (rated answers)
and leads (handoff captures). We do NOT log every user query's text elsewhere, so "top questions"
is bounded by what users explicitly submitted via feedback/lead forms. This is intentional and
documented; full query-text mining would require a separate (privacy-reviewed) logging decision.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from .db import get_session_maker


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def get_out_of_scope_rate(tenant_id: str, *, days: int = 30) -> dict:
    """Fraction of queries flagged out_of_scope, from the usage_events meta column."""
    start = _now() - timedelta(days=days)
    session_maker = get_session_maker()
    async with session_maker() as session:
        rows = (await session.execute(text("""
            SELECT meta FROM usage_events
            WHERE tenant_id = :tenant_id AND kind = 'query' AND ts >= :start
        """), {"tenant_id": tenant_id, "start": start})).fetchall()
    total = 0
    oos = 0
    for (meta_json,) in rows:
        try:
            import json
            m = json.loads(meta_json) if meta_json else {}
        except Exception:
            m = {}
        total += 1
        if m.get("out_of_scope"):
            oos += 1
    return {
        "queries": total,
        "out_of_scope": oos,
        "out_of_scope_rate": (oos / total) if total else None,
    }


async def get_top_questions(tenant_id: str, *, limit: int = 10, days: int = 30) -> list[dict]:
    """Top user questions by frequency, derived from captured feedback + lead question text.

    Honest scope: only questions users explicitly submitted via a feedback or handoff form are
    available (we don't log every query's text). Returns [{question, count, source}].
    """
    start = _now() - timedelta(days=days)
    session_maker = get_session_maker()
    counts: Counter = Counter()
    async with session_maker() as session:
        fb = (await session.execute(text("""
            SELECT question FROM feedback
            WHERE tenant_id = :tenant_id AND question IS NOT NULL AND ts >= :start
        """), {"tenant_id": tenant_id, "start": start})).fetchall()
        for (q,) in fb:
            if q and q.strip():
                counts[q.strip()] += 1
        ld = (await session.execute(text("""
            SELECT question FROM leads
            WHERE tenant_id = :tenant_id AND question IS NOT NULL AND ts >= :start
        """), {"tenant_id": tenant_id, "start": start})).fetchall()
        for (q,) in ld:
            if q and q.strip():
                counts[q.strip()] += 1
    return [{"question": q, "count": c} for q, c in counts.most_common(limit)]


async def get_tenant_analytics(tenant_id: str, *, days: int = 30) -> dict:
    """Unified per-tenant analytics aggregate (issue #37)."""
    from .usage import get_usage_summary, get_usage_timeseries
    from .feedback import get_feedback_summary
    from .leads import get_lead_summary

    usage = await get_usage_summary(tenant_id, days=days)
    oos = await get_out_of_scope_rate(tenant_id, days=days)
    feedback = await get_feedback_summary(tenant_id)
    leads = await get_lead_summary(tenant_id)
    top_questions = await get_top_questions(tenant_id, days=days)

    # handoff -> lead funnel: out-of-scope queries that became captured leads.
    handoff_funnel = {
        "out_of_scope_queries": oos["out_of_scope"],
        "leads_captured": leads["total"],
        "capture_rate": (leads["total"] / oos["out_of_scope"]) if oos["out_of_scope"] else None,
    }

    return {
        "tenant_id": tenant_id,
        "period_days": days,
        "period_start": usage.period_start.isoformat(),
        "period_end": usage.period_end.isoformat(),
        "usage": {
            "queries": usage.queries,
            "docs_ingested": usage.docs_ingested,
            "chunks_ingested": usage.chunks_ingested,
            "eval_runs": usage.eval_runs,
            "by_kind": usage.by_kind,
        },
        "out_of_scope": oos,
        "feedback": feedback,
        "leads": leads,
        "handoff_funnel": handoff_funnel,
        "top_questions": top_questions,
    }


async def get_analytics_csv_rows(tenant_id: str, *, days: int = 30) -> list[dict]:
    """Flat rows for CSV export: one row per daily timeseries entry, enriched with the
    aggregate headline metrics as trailing columns."""
    from .usage import get_usage_timeseries
    a = await get_tenant_analytics(tenant_id, days=days)
    series = await get_usage_timeseries(tenant_id, days=days)
    fb = a["feedback"]
    positive = (fb["positive_rate"] if fb["positive_rate"] is not None else "")
    oos_rate = (a["out_of_scope"]["out_of_scope_rate"] if a["out_of_scope"]["out_of_scope_rate"] is not None else "")
    cap_rate = (a["handoff_funnel"]["capture_rate"] if a["handoff_funnel"]["capture_rate"] is not None else "")
    rows = []
    for day in series:
        rows.append({
            "date": day["date"],
            "queries": day["queries"],
            "docs_ingested": day["docs_ingested"],
            "chunks_ingested": day["chunks_ingested"],
            "eval_runs": day["eval_runs"],
            "out_of_scope_rate": oos_rate,
            "feedback_up": fb["up"],
            "feedback_down": fb["down"],
            "leads_total": a["leads"]["total"],
            "handoff_capture_rate": cap_rate,
        })
    return rows
