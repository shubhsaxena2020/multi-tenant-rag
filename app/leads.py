"""Human-handoff / lead capture for out-of-scope queries (PHASE D, issue #33).

When the RAG service can't answer (out-of-scope / low confidence), the widget shows a graceful
handoff. This module turns that lost query into a *captured lead* the business can follow up on:
the user's question plus a way to reach them (email or phone) and an optional message. Append-only,
tiny rows, reliable (one insert per lead). Distinct from the sampled audit trail.

Table `leads`:
  id, tenant_id, session_id, question, name, email, phone, message, ts
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from sqlalchemy import text

from .db import get_session_maker

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _valid_email(email: str | None) -> bool:
    return bool(email) and bool(_EMAIL_RE.match(email)) and len(email) <= 320


def _valid_phone(phone: str | None) -> bool:
    # Permissive: digits, spaces, + ( ) - . and at least 7 digits. Avoids hard-validation
    # edge cases across countries while rejecting obvious garbage.
    if not phone:
        return False
    if len(phone) > 50:
        return False
    digits = re.sub(r"\D", "", phone)
    return len(digits) >= 7


async def save_lead(
    tenant_id: str,
    *,
    question: str,
    name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    message: str | None = None,
    session_id: str | None = None,
) -> int:
    """Persist a handoff lead. Requires a reachable contact (valid email OR valid phone) — a lead
    with no way to reach the user is useless. Raises ValueError on invalid contact."""
    email = (email or "").strip() or None
    phone = (phone or "").strip() or None
    if not _valid_email(email) and not _valid_phone(phone):
        raise ValueError("a valid email or phone is required to capture a lead")
    if email and not _valid_email(email):
        raise ValueError("invalid email")
    if phone and not _valid_phone(phone):
        raise ValueError("invalid phone")
    session_maker = get_session_maker()
    async with session_maker() as session:
        await session.execute(text("""
            CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT NOT NULL,
                session_id TEXT,
                question TEXT,
                name TEXT,
                email TEXT,
                phone TEXT,
                message TEXT,
                ts TIMESTAMP WITH TIME ZONE NOT NULL
            )
        """))
        result = await session.execute(text("""
            INSERT INTO leads (tenant_id, session_id, question, name, email, phone, message, ts)
            VALUES (:tenant_id, :session_id, :question, :name, :email, :phone, :message, :ts)
        """), {
            "tenant_id": tenant_id,
            "session_id": session_id,
            "question": question,
            "name": (name or "").strip() or None,
            "email": email,
            "phone": phone,
            "message": (message or "").strip() or None,
            "ts": _now(),
        })
        row_id = (await session.execute(text("SELECT last_insert_rowid()"))).scalar()
        await session.commit()
        return int(row_id)


async def list_leads(tenant_id: str, *, limit: int = 100) -> list[dict]:
    session_maker = get_session_maker()
    async with session_maker() as session:
        rows = (await session.execute(text("""
            SELECT id, tenant_id, session_id, question, name, email, phone, message, ts
            FROM leads WHERE tenant_id = :tenant_id ORDER BY ts DESC LIMIT :limit
        """), {"tenant_id": tenant_id, "limit": limit})).fetchall()
    return [{
        "id": r[0], "tenant_id": r[1], "session_id": r[2], "question": r[3],
        "name": r[4], "email": r[5], "phone": r[6], "message": r[7], "ts": str(r[8]),
    } for r in rows]


async def get_lead_summary(tenant_id: str) -> dict:
    session_maker = get_session_maker()
    async with session_maker() as session:
        total = (await session.execute(text(
            "SELECT COUNT(*) FROM leads WHERE tenant_id = :tenant_id"),
            {"tenant_id": tenant_id})).scalar() or 0
        with_email = (await session.execute(text(
            "SELECT COUNT(*) FROM leads WHERE tenant_id = :tenant_id AND email IS NOT NULL"),
            {"tenant_id": tenant_id})).scalar() or 0
        with_phone = (await session.execute(text(
            "SELECT COUNT(*) FROM leads WHERE tenant_id = :tenant_id AND phone IS NOT NULL"),
            {"tenant_id": tenant_id})).scalar() or 0
    return {
        "tenant_id": tenant_id,
        "total": int(total),
        "with_email": int(with_email),
        "with_phone": int(with_phone),
    }
