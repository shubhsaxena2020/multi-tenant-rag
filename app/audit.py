"""Tamper-evident audit log (append-only, SHA-256 hash-chained).

Records privileged admin actions (tenant lifecycle, key rotation/revocation,
authorization failures) in a way that makes retroactive editing detectable: each
row carries the SHA-256 hash of the previous row's canonical payload, so any
insert/reorder/edit in the middle of the chain breaks the link.

Security posture:
- **Fail-open**: audit is best-effort. If the DB write fails, the *primary*
  operation (e.g. tenant creation) MUST still succeed — we never let logging
  block or crash a request. We log a warning instead.
- **No tenant data**: only structural admin actions + actor identity are recorded.
  This deliberately does NOT touch the tenant-key-derived isolation path
  (auth.py / vector_store.py tenant_id filters) — it is purely observational.
- **Verification** is exposed via the admin-gated `/audit` and `/audit/verify`
  endpoints so an operator can prove integrity.

Storage: same async SQLAlchemy engine as the tenant registry (SQLite dev / PG
fleet). A single ordered table per deployment is sufficient; per-row `prev_hash`
provides the chain, and `id` (autoincrement) provides deterministic ordering.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Integer, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, get_session_maker

log = logging.getLogger("rag.audit")

# Field set that participates in the chain hash (pinned; json.dumps sort_keys keeps order stable).
_CHAIN_KEYS = ("id", "ts", "action", "actor", "target", "meta", "prev_hash")


def _canonical(payload: dict) -> str:
    """Stable serialization of a row's chain-relevant content."""
    return json.dumps(
        {k: payload[k] for k in _CHAIN_KEYS},
        sort_keys=True,
        separators=(",", ":"),
    )


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def get_last_audit_hash(session: Any = None) -> str:
    """Return the hash of the most recent audit row (or the genesis sentinel)."""
    maker = get_session_maker()
    async with (session or maker()) as s:
        stmt = select(AuditLog).order_by(AuditLog.id.desc()).limit(1)
        res = await s.execute(stmt)
        row = res.scalar_one_or_none()
        if row is None:
            return "GENESIS"
        return row.row_hash


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    target: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    meta: Mapped[str] = mapped_column(Text, nullable=False, default="")
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="GENESIS")
    row_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    def chain_payload(self) -> dict:
        # Normalize ts to a naive UTC ISO string so the on-disk representation (SQLite
        # returns DateTime(timezone=True) as naive) matches the insert-time value exactly
        # — a tz-aware vs naive mismatch would silently break the hash chain.
        ts = self.ts
        if isinstance(ts, datetime):
            ts = ts.replace(tzinfo=None).isoformat()
        else:
            ts = str(ts)
        return {
            "id": self.id,
            "ts": ts,
            "action": self.action,
            "actor": self.actor,
            "target": self.target,
            "meta": self.meta,
            "prev_hash": self.prev_hash,
        }


async def append_audit(
    action: str,
    actor: str,
    target: str = "",
    meta: dict[str, Any] | None = None,
    session: Any = None,
) -> None:
    """Append a hash-chained audit entry. Fail-open: never raises to the caller.

    Pass the *current* DB session when one already exists (e.g. inside an admin
    route) so the audit row commits in the same transaction where possible.
    """
    try:
        meta_str = json.dumps(meta or {}, sort_keys=True, separators=(",", ":"))
        # Store as naive UTC (SQLite DateTime(timezone=True) returns naive on read), so the
        # canonical payload is byte-identical at insert time and at verify time.
        now = datetime.now(UTC).replace(tzinfo=None)
        prev = await get_last_audit_hash(session)
        maker = get_session_maker()
        async with (session or maker()) as s:
            row = AuditLog(
                ts=now,
                action=action,
                actor=actor,
                target=target,
                meta=meta_str,
                prev_hash=prev,
                row_hash="",  # filled after we know the assigned id
            )
            s.add(row)
            await s.flush()  # assigns row.id
            row.row_hash = _hash(_canonical(row.chain_payload()))
            await s.commit()
    except Exception:
        # Fail-open: audit must never break the primary operation, BUT a gap in the
        # accountability trail must not go unnoticed (P1 #6). Surface it via a metric
        # (Prometheus/Alertmanager) and a structured warning with the exception type so
        # an operator can alert on sustained audit-write failures.
        from .observability import AUDIT_FAILURES

        log.warning(
            "audit_append_failed",
            extra={"action": action, "actor": actor, "error_type": "audit_write_error"},
        )
        AUDIT_FAILURES.labels(action=action).inc()


async def list_audit(limit: int = 200, session: Any = None) -> list[dict]:
    """Return audit rows ordered by id (oldest first) so the chain reads top-to-bottom."""
    maker = get_session_maker()
    async with (session or maker()) as s:
        stmt = select(AuditLog).order_by(AuditLog.id.asc()).limit(limit)
        res = await s.execute(stmt)
        rows = res.scalars().all()
        return [
            {
                "id": r.id,
                "ts": r.ts.isoformat() if isinstance(r.ts, datetime) else str(r.ts),
                "action": r.action,
                "actor": r.actor,
                "target": r.target,
                "meta": json.loads(r.meta or "{}"),
                "prev_hash": r.prev_hash,
                "row_hash": r.row_hash,
            }
            for r in rows
        ]


async def verify_chain(session: Any = None) -> dict:
    """Walk the chain and confirm every row's prev_hash links to the previous row_hash.

    Returns {ok, first_break_id, checked}. `ok=False` + `first_break_id` points at the
    first tampered row (whose prev_hash doesn't match the prior hash, or whose own
    row_hash doesn't recompute).
    """
    rows = await list_audit(limit=10_000_000, session=session)
    expected_prev = "GENESIS"
    for i, r in enumerate(rows):
        # 1) link integrity: this row must point at the previous row's hash
        if r["prev_hash"] != expected_prev:
            return {"ok": False, "first_break_id": r["id"], "checked": i}
        # 2) self integrity: recompute this row's hash from its canonical payload
        canon = _canonical(
            {
                "id": r["id"],
                "ts": r["ts"],
                "action": r["action"],
                "actor": r["actor"],
                "target": r["target"],
                "meta": json.dumps(r["meta"], sort_keys=True, separators=(",", ":")),
                "prev_hash": r["prev_hash"],
            }
        )
        if _hash(canon) != r["row_hash"]:
            return {"ok": False, "first_break_id": r["id"], "checked": i}
        expected_prev = r["row_hash"]
    return {"ok": True, "first_break_id": None, "checked": len(rows)}
