"""Per-tenant answer-quality feedback capture (PHASE D: business value, issue #29).

Users can signal whether an answer was helpful (thumbs up/down) from the widget. Each rating is
stored with the question/answer/session context so operators can measure answer quality over time
and spot weak areas. Append-only, tiny rows, reliable (one insert, not sampled).

Table `feedback`:
  id, tenant_id, session_id, message_id, rating ('up'|'down'), comment, question, answer, ts
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import text

from .db import get_session_maker

_VALID = {"up", "down"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def save_feedback(
    tenant_id: str,
    *,
    rating: str,
    session_id: str | None = None,
    message_id: str | None = None,
    comment: str | None = None,
    question: str | None = None,
    answer: str | None = None,
) -> int:
    """Persist a feedback rating. `rating` must be 'up' or 'down' (validated by the caller/model).
    Returns the new row id."""
    rating = (rating or "").lower()
    if rating not in _VALID:
        raise ValueError(f"rating must be one of {sorted(_VALID)}")
    session_maker = get_session_maker()
    async with session_maker() as session:
        await session.execute(text("""
            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT NOT NULL,
                session_id TEXT,
                message_id TEXT,
                rating TEXT NOT NULL,
                comment TEXT,
                question TEXT,
                answer TEXT,
                ts TIMESTAMP WITH TIME ZONE NOT NULL
            )
        """))
        result = await session.execute(text("""
            INSERT INTO feedback (tenant_id, session_id, message_id, rating, comment, question, answer, ts)
            VALUES (:tenant_id, :session_id, :message_id, :rating, :comment, :question, :answer, :ts)
        """), {
            "tenant_id": tenant_id,
            "session_id": session_id,
            "message_id": message_id,
            "rating": rating,
            "comment": comment,
            "question": question,
            "answer": answer,
            "ts": _now(),
        })
        row_id = (await session.execute(text("SELECT last_insert_rowid()"))).scalar()
        await session.commit()
        return int(row_id)


async def list_feedback(tenant_id: str, *, limit: int = 100, rating: str | None = None) -> list[dict]:
    session_maker = get_session_maker()
    async with session_maker() as session:
        sql = """
            SELECT id, tenant_id, session_id, message_id, rating, comment, question, answer, ts
            FROM feedback WHERE tenant_id = :tenant_id
        """
        params: dict = {"tenant_id": tenant_id}
        if rating:
            sql += " AND rating = :rating"
            params["rating"] = rating.lower()
        sql += " ORDER BY ts DESC LIMIT :limit"
        params["limit"] = limit
        rows = (await session.execute(text(sql), params)).fetchall()
    return [{
        "id": r[0], "tenant_id": r[1], "session_id": r[2], "message_id": r[3],
        "rating": r[4], "comment": r[5], "question": r[6], "answer": r[7],
        "ts": str(r[8]),
    } for r in rows]


async def get_feedback_summary(tenant_id: str) -> dict:
    session_maker = get_session_maker()
    async with session_maker() as session:
        rows = (await session.execute(text("""
            SELECT rating, COUNT(*) FROM feedback WHERE tenant_id = :tenant_id GROUP BY rating
        """), {"tenant_id": tenant_id})).fetchall()
    counts = {r[0]: int(r[1]) for r in rows}
    up = counts.get("up", 0)
    down = counts.get("down", 0)
    total = up + down
    return {
        "tenant_id": tenant_id,
        "up": up,
        "down": down,
        "total": total,
        "positive_rate": (up / total) if total else None,
    }
