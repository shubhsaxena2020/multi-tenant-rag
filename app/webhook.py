"""PHASE D.2: best-effort outbound webhook for human-handoff lead capture.

Generic, provider-agnostic POST. Live endpoint URLs are per-tenant operator config
(human checkpoint) — by default no webhook is configured and leads are stored only.
Failures are swallowed (the DB row is the source of truth); a webhook outage must
NEVER lose a lead or fail the inbound request.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

log = logging.getLogger("rag")


async def dispatch_lead_webhook(webhook_url: str, payload: dict, timeout: float = 8.0) -> bool:
    """POST the lead payload to the tenant's webhook. Returns True on success.

    Any error (network, non-2xx, timeout) is logged and returns False — never raises.
    """
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                webhook_url,
                json=payload,
                headers={"Content-Type": "application/json", "User-Agent": "rag-service-lead/1.0"},
            )
        ok = 200 <= resp.status_code < 300
        if not ok:
            log.warning("lead_webhook_non_2xx", extra={"status": resp.status_code, "url": webhook_url})
        return ok
    except Exception as e:  # noqa: BLE001 — best-effort; lead is already stored
        log.warning("lead_webhook_failed", extra={"error_type": type(e).__name__, "url": webhook_url})
        return False


def dispatch_lead_webhook_sync(webhook_url: str, payload: dict, timeout: float = 8.0) -> bool:
    """Synchronous wrapper for use inside a sync endpoint (runs the async helper)."""
    try:
        return asyncio.run(dispatch_lead_webhook(webhook_url, payload, timeout))
    except RuntimeError:
        # Already inside an event loop (FastAPI). Schedule cooperatively.
        loop = asyncio.get_event_loop()
        return loop.run_until_complete(dispatch_lead_webhook(webhook_url, payload, timeout))
