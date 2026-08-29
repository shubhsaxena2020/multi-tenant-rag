"""Unanswered-question / knowledge-gap logging (PHASE E, issue #13).

When a query is out-of-scope (no grounding in the tenant's corpus) it is a *knowledge gap* —
the user wanted something the RAG can't answer. We persist these so operators can mine gaps and
improve the corpus. Distinct from `leads`: a lead is only when the user *also* chose to leave
contact info via the handoff card; a gap is recorded for every out-of-scope query regardless of
whether the user submitted contact details.

Security boundary: gaps are recorded ONLY for out-of-scope (low-retrieval-confidence) queries,
NOT for injection-blocked queries. An injection attempt is a security signal, not a knowledge
gap, and must not be conflated with legitimate missing-content analysis. The classification is
already computed in app/conversation.py (assess_confidence / detect_injection); this module only
routes it. No P0/P1 behavior is changed.

Table `knowledge_gaps`: id, tenant_id, session_id, question, ts
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from .db import get_session_maker


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _ensure_table(session) -> None:
    await session.execute(text("""
        CREATE TABLE IF NOT EXISTS knowledge_gaps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL,
            session_id TEXT,
            question TEXT,
            ts TIMESTAMP WITH TIME ZONE NOT NULL
        )
    """))


async def record_knowledge_gap(
    tenant_id: str,
    *,
    question: str | None = None,
    session_id: str | None = None,
) -> int | None:
    """Persist an out-of-scope question as a knowledge gap. Best-effort: returns the new row id
    or None. Skips empty questions. Never raises (metering-like reliability)."""
    q = (question or "").strip()
    if not q:
        return None
    try:
        session_maker = get_session_maker()
        async with session_maker() as session:
            await _ensure_table(session)
            result = await session.execute(text("""
                INSERT INTO knowledge_gaps (tenant_id, session_id, question, ts)
                VALUES (:tenant_id, :session_id, :question, :ts)
            """), {
                "tenant_id": tenant_id,
                "session_id": session_id,
                "question": q,
                "ts": _now(),
            })
            row_id = (await session.execute(text("SELECT last_insert_rowid()"))).scalar()
            await session.commit()
            return int(row_id) if row_id is not None else None
    except Exception:
        # metering-like: a gap log failure must never break the chat response.
        return None


def record_knowledge_gap_bg(tenant_id: str, *, question: str | None = None, session_id: str | None = None) -> None:
    """Fire-and-forget variant for sync contexts / SSE worker threads (no running loop)."""
    import threading

    def _run() -> None:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(record_knowledge_gap(tenant_id, question=question, session_id=session_id))
        except Exception:  # pragma: no cover - must never crash the request
            pass
        finally:
            loop.close()

    threading.Thread(target=_run, daemon=True).start()


async def list_knowledge_gaps(tenant_id: str, *, limit: int = 100, days: int | None = None) -> list[dict]:
    """Recent knowledge gaps for a tenant, most-recent first. Table is ensured so this is safe
    even before any gap has been recorded."""
    session_maker = get_session_maker()
    async with session_maker() as session:
        await _ensure_table(session)
        sql = """
            SELECT id, tenant_id, session_id, question, ts
            FROM knowledge_gaps WHERE tenant_id = :tenant_id
        """
        params: dict = {"tenant_id": tenant_id, "limit": limit}
        if days:
            from datetime import timedelta
            sql += " AND ts >= :start"
            params["start"] = _now() - timedelta(days=days)
        sql += " ORDER BY ts DESC LIMIT :limit"
        rows = (await session.execute(text(sql), params)).fetchall()
    return [{
        "id": r[0], "tenant_id": r[1], "session_id": r[2],
        "question": r[3], "ts": str(r[4]),
    } for r in rows]


async def count_knowledge_gaps(tenant_id: str) -> int:
    session_maker = get_session_maker()
    async with session_maker() as session:
        await _ensure_table(session)
        n = (await session.execute(text(
            "SELECT COUNT(*) FROM knowledge_gaps WHERE tenant_id = :tenant_id"),
            {"tenant_id": tenant_id})).scalar() or 0
    return int(n)
