"""Per-tenant token usage + cost metering (PHASE E, issue #14).

Every generated answer carries an OpenAI-style usage dict (prompt/completion/total tokens). We
persist per-tenant token consumption so operators can bill/quota by actual LLM spend. When no LLM
provider is configured the generation path reports a deterministic *estimated* usage (the service
stays self-hostable without a paid key, per the explicit human checkpoint); the `estimated` flag in
the stored row marks it so cost math can be caveated.

Cost is computed from a configurable per-1k-token price (default $0.00 — operator sets real prices;
we never invent a paid key or hard-coded price that implies a real charge). Billing integration
itself is an explicit human checkpoint: we implement ONLY usage counters + quota logic.

Table `token_usage`: id, tenant_id, session_id, prompt_tokens, completion_tokens, total_tokens,
estimated (bool), model, ts
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from .config import get_settings
from .db import get_session_maker


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _ensure_table(session) -> None:
    await session.execute(text("""
        CREATE TABLE IF NOT EXISTS token_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL,
            session_id TEXT,
            prompt_tokens INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            estimated INTEGER NOT NULL DEFAULT 0,
            model TEXT,
            ts TIMESTAMP WITH TIME ZONE NOT NULL
        )
    """))


async def record_token_usage(
    tenant_id: str,
    *,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    estimated: bool = False,
    model: str | None = None,
    session_id: str | None = None,
) -> None:
    """Persist token consumption. Best-effort (never raises)."""
    if total_tokens <= 0 and (prompt_tokens <= 0 and completion_tokens <= 0):
        return
    try:
        session_maker = get_session_maker()
        async with session_maker() as session:
            await _ensure_table(session)
            await session.execute(text("""
                INSERT INTO token_usage
                    (tenant_id, session_id, prompt_tokens, completion_tokens, total_tokens, estimated, model, ts)
                VALUES
                    (:tenant_id, :session_id, :prompt_tokens, :completion_tokens, :total_tokens, :estimated, :model, :ts)
            """), {
                "tenant_id": tenant_id,
                "session_id": session_id,
                "prompt_tokens": int(prompt_tokens),
                "completion_tokens": int(completion_tokens),
                "total_tokens": int(total_tokens),
                "estimated": 1 if estimated else 0,
                "model": model,
                "ts": _now(),
            })
            await session.commit()
    except Exception:
        return


def record_token_usage_bg(
    tenant_id: str, *, prompt_tokens: int = 0, completion_tokens: int = 0,
    total_tokens: int = 0, estimated: bool = False, model: str | None = None,
    session_id: str | None = None,
) -> None:
    """Fire-and-forget variant for the SSE worker thread (no running loop)."""
    import threading

    def _run() -> None:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(record_token_usage(
                tenant_id, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                total_tokens=total_tokens, estimated=estimated, model=model, session_id=session_id,
            ))
        except Exception:  # pragma: no cover - must never crash the request
            pass
        finally:
            loop.close()

    threading.Thread(target=_run, daemon=True).start()


def _price_per_1k(settings) -> tuple[float, float]:
    """(prompt_price_per_1k, completion_price_per_1k). Operator-set; default 0.0 (no invented cost)."""
    return float(getattr(settings, "token_price_prompt_per_1k", 0.0) or 0.0), \
           float(getattr(settings, "token_price_completion_per_1k", 0.0) or 0.0)


async def get_token_usage(tenant_id: str, *, days: int = 30) -> dict:
    """Per-tenant token + cost rollup for the window. Table ensured so safe pre-first-use."""
    s = get_settings()
    prompt_price, completion_price = _price_per_1k(s)
    session_maker = get_session_maker()
    async with session_maker() as session:
        await _ensure_table(session)
        from datetime import timedelta
        start = _now() - timedelta(days=days)
        row = (await session.execute(text("""
            SELECT COALESCE(SUM(prompt_tokens),0), COALESCE(SUM(completion_tokens),0),
                   COALESCE(SUM(total_tokens),0), COUNT(*),
                   COALESCE(SUM(estimated),0)
            FROM token_usage WHERE tenant_id = :tenant_id AND ts >= :start
        """), {"tenant_id": tenant_id, "start": start})).fetchone() or (0, 0, 0, 0, 0)
    prompt_t, completion_t, total_t, calls, est = (int(row[0]), int(row[1]), int(row[2]), int(row[3]), int(row[4]))
    est_cost = (prompt_t / 1000.0) * prompt_price + (completion_t / 1000.0) * completion_price
    return {
        "tenant_id": tenant_id,
        "window_days": days,
        "calls": calls,
        "prompt_tokens": prompt_t,
        "completion_tokens": completion_t,
        "total_tokens": total_t,
        "estimated_rows": est,
        "cost_usd": round(est_cost, 6),
        "cost_basis": "operator_set_price_per_1k" if (prompt_price or completion_price) else "no_price_set",
        "price_prompt_per_1k": prompt_price,
        "price_completion_per_1k": completion_price,
    }


async def get_fleet_token_usage(*, days: int = 30) -> dict:
    """Fleet-wide rollup across all tenants (for the summary/analytics endpoint #15).

    Returns token usage aggregated over the window. Note: tenant_count reflects
    tenants who have token usage records, not the total fleet tenant count - that
    information is available from /admin/summary's top-level tenant_count.
    """
    s = get_settings()
    prompt_price, completion_price = _price_per_1k(s)
    session_maker = get_session_maker()
    async with session_maker() as session:
        await _ensure_table(session)
        from datetime import timedelta
        start = _now() - timedelta(days=days)
        row = (await session.execute(text("""
            SELECT COALESCE(SUM(prompt_tokens),0), COALESCE(SUM(completion_tokens),0),
                   COALESCE(SUM(total_tokens),0), COUNT(*), COUNT(DISTINCT tenant_id)
            FROM token_usage WHERE ts >= :start
        """"), {"start": start})).fetchone() or (0, 0, 0, 0, 0)
    prompt_t, completion_t, total_t, calls, tenants = (int(row[0]), int(row[1]), int(row[2]), int(row[3]), int(row[4]))
    est_cost = (prompt_t / 1000.0) * prompt_price + (completion_t / 1000.0) * completion_price
    return {
        "window_days": days,
        "tenant_count": tenants,
        "calls": calls,
        "prompt_tokens": prompt_t,
        "completion_tokens": completion_t,
        "total_tokens": total_t,
        "cost_usd": round(est_cost, 6),
        "cost_basis": "operator_set_price_per_1k" if (prompt_price or completion_price) else "no_price_set",
    }
