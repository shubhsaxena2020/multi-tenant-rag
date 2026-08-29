"""Outbound webhooks for tenant-provided callback URLs.

Two consumers:
  * PHASE D.2 lead webhook — POST a human-handoff lead to the tenant's CRM/Slack/etc.
  * v10.6 generic ingestion webhook — POST job completion/failure to the tenant's
    automation so they don't have to poll job status.

SECURITY (the whole point of v10.6): a tenant-supplied webhook URL is an outbound
egress target controlled by an *untrusted* party (the tenant). Without a guard, a
tenant could point it at http://169.254.169.254/ (cloud IMDS) or http://localhost:8080/
to scan our internal network — a confused-deputy SSRF. So EVERY egress URL is validated
by app.ingestion.ssrf.validate_url_for_egress (public-only, ports 80/443, ip-pinned) at
BOTH config time (reject) and dispatch time (defense in depth). Production egress never
hits private/loopback (WEBHOOK_ALLOW_PRIVATE defaults off; tests enable it locally).

Payloads are HMAC-SHA256 signed with the operator master key (MASTER_ENCRYPTION_KEY) so
the tenant can verify the callback genuinely came from this service and not a forger.
When no master key is set (dev) we send unsigned with a warning (fail-open on signing,
never on SSRF).

Failures are swallowed: the DB row / job status is the source of truth, and a webhook
outage must NEVER lose a lead or fail an ingestion job.
"""
from __future__ import annotations

import hashlib
import hmac
import logging

import httpx
from urllib.parse import urlparse

from .config import get_settings
from .ingestion.ssrf import validate_url_for_egress

log = logging.getLogger("rag")

# Header carrying the HMAC signature (hex of HMAC-SHA256(key, body)).
SIG_HEADER = "X-Rag-Signature"
# Header carrying the tenant id so the receiver can look up the shared secret / verify.
TENANT_HEADER = "X-Rag-Tenant"


def _signing_key() -> bytes | None:
    """Master key used to sign webhook payloads. None when unset (dev, unsigned)."""
    raw = (get_settings().master_encryption_key or "").strip()
    if not raw:
        return None
    # Reuse the same coercion as crypto: base64 / 64-hex / raw-truncated.
    from .crypto import _coerce_key

    return _coerce_key(raw)


def sign_payload(secret: bytes, body: bytes) -> str:
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


def _build_headers(tenant_id: str, body: bytes) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "rag-service-webhook/1.0",
        TENANT_HEADER: tenant_id,
    }
    key = _signing_key()
    if key is not None:
        headers[SIG_HEADER] = sign_payload(key, body)
    else:
        log.warning("webhook_unsigned_no_master_key", extra={"tenant_id": tenant_id})
    return headers


def _egress_allowed_private() -> bool:
    # Env escape for local test harnesses only. Default False in production.
    return (get_settings().webhook_allow_private
            if hasattr(get_settings(), "webhook_allow_private")
            else False)


async def dispatch_webhook(
    webhook_url: str,
    payload: dict,
    tenant_id: str,
    *,
    timeout: float = 8.0,
    allow_private: bool | None = None,
) -> bool:
    """POST a JSON payload to a tenant's webhook. SSRF-guarded + signed.

    Returns True on 2xx. Any error (SSRF block, network, non-2xx, timeout, signing
    issue) is logged and returns False — never raises, so the caller's primary path
    (lead storage / ingestion job) is never affected.
    """
    allow = _egress_allowed_private() if allow_private is None else allow_private
    # Re-validate at dispatch time (defense in depth; config-time already rejected bad URLs).
    try:
        scheme, pinned_ip, port = validate_url_for_egress(webhook_url, allow_private=allow)
    except ValueError as e:
        log.warning("webhook_ssrf_blocked",
                    extra={"url": webhook_url, "error": str(e), "tenant_id": tenant_id})
        return False

    body = _json_dumps(payload)
    headers = _build_headers(tenant_id, body.encode())
    # IP-pin: connect to the exact validated address to defeat DNS rebinding / TOCTOU.
    from urllib.parse import urlparse as _up

    p = _up(webhook_url)
    host_header = p.hostname or pinned_ip
    pinned_url = f"{scheme}://{pinned_ip}:{port}{p.path or '/'}"
    if p.query:
        pinned_url += "?" + p.query
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                pinned_url,
                content=body,
                headers={**headers, "Host": host_header},
            )
        ok = 200 <= resp.status_code < 300
        if not ok:
            log.warning("webhook_non_2xx",
                        extra={"status": resp.status_code, "url": webhook_url,
                               "tenant_id": tenant_id})
        return ok
    except Exception as e:  # noqa: BLE001 — best-effort; primary path is untouched
        log.warning("webhook_failed",
                    extra={"error_type": type(e).__name__, "url": webhook_url,
                           "tenant_id": tenant_id})
        return False


def _json_dumps(payload: dict) -> str:
    import json

    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def dispatch_lead_webhook(webhook_url: str, payload: dict, tenant_id: str,
                          timeout: float = 8.0) -> bool:
    """PHASE D.2 lead webhook. Sync wrapper; runs the async dispatch."""
    import asyncio

    try:
        return asyncio.run(dispatch_webhook(webhook_url, payload, tenant_id, timeout=timeout))
    except RuntimeError:
        loop = asyncio.get_event_loop()
        return loop.run_until_complete(
            dispatch_webhook(webhook_url, payload, tenant_id, timeout=timeout))
