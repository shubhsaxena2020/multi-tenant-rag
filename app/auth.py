"""Resolve tenant identity from a Bearer API key on every request.

Security model (Truto 2026 multi-tenant isolation): tenant_id is derived
server-side from the verified API key and is NEVER trusted from the request body.
All downstream Qdrant operations are scoped to the tenant's own collection, so
cross-tenant reads are structurally impossible.
"""
from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Header, HTTPException, status

from . import tenants
from .config import get_settings


def generate_api_key() -> str:
    return "rk_" + secrets.token_urlsafe(32)


def generate_publishable_key() -> str:
    """P1 #9: read-only key (pk_*) safe to embed client-side in the widget.

    It resolves to the SAME tenant as a secret key (isolation is unchanged) but is
    scope-locked to query endpoints by require_secret_key().
    """
    return "pk_" + secrets.token_urlsafe(32)


async def get_tenant_from_header(
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> tenants.TenantRow:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header",
        )
    key = authorization.split(" ", 1)[1].strip()
    if not key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Empty API key")
    tenant = await tenants.get_tenant_by_key(key)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
    return tenant


async def require_secret_key(
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> None:
    """P1 #9: gate write/admin routes to full-power SECRET keys (rk_*).

    A PUBLISHABLE key (pk_*) must be rejected from ingest/admin/rotate/revoke/delete
    so a leaked client-side widget key cannot poison or reconfigure a tenant. Query
    endpoints intentionally do NOT use this dependency, so publishable keys can read.
    The tenant_id is still derived server-side from the key (no body trust); this only
    adds a scope check on the key tier.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header",
        )
    key = authorization.split(" ", 1)[1].strip()
    kind = await tenants.get_key_kind(key)
    if kind != "secret":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Publishable key is read-only; use a secret key for this operation",
        )


async def require_secret_or_publishable(
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> None:
    """PHASE D.1: allow BOTH secret (rk_*) and publishable (pk_*) keys for benign,
    non-destructive interactions the widget performs client-side (e.g. thumbs feedback).
    Anything destructive still uses require_secret_key(); this never grants write power,
    only lets the embeddable widget record a rating with its publishable key.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header",
        )
    key = authorization.split(" ", 1)[1].strip()
    kind = await tenants.get_key_kind(key)
    if kind is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")


def require_admin(
    admin_key: Annotated[str | None, Header(alias="Admin-Key")] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Guard tenant admin operations. If ADMIN_API_KEY is configured, the
    Admin-Key header (or `Authorization: Bearer <key>`) must match; otherwise
    (dev) admin is open. Accepting the standard Authorization Bearer form lets
    Prometheus scrape /metrics via its native `authorization` block."""
    settings = get_settings()
    if not settings.admin_api_key:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Admin API key not set"
        )
    provided = admin_key
    if authorization and authorization.lower().startswith("bearer "):
        provided = authorization.split(" ", 1)[1].strip()
    if provided != settings.admin_api_key:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Admin key required"
        )
