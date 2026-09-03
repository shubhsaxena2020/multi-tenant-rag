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
    authorization: str | None = Header(default=None),
) -> tenants.TenantRow:
    # Normalize: if dict received (e.g. from TestClient), extract string auth value
    if isinstance(authorization, dict):
        auth_val = authorization.get("authorization") or authorization.get("Authorization")
        if auth_val is not None:
            authorization = auth_val
    if not authorization or not str(authorization).lower().startswith("bearer "):
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
    authorization: str | None = Header(default=None),
) -> None:
    """P1 #9: gate write/admin routes to full-power SECRET keys (rk_*).

    A PUBLISHABLE key (pk_*) must be rejected from ingest/admin/rotate/revoke/delete
    so a leaked client-side widget key cannot poison or reconfigure a tenant. Query
    endpoints intentionally do NOT use this dependency, so publishable keys can read.
    The tenant_id is still derived server-side from the key (no body trust); this only
    adds a scope check on the key tier.
    """
    if not authorization or not str(authorization).lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header",
        )
    key = authorization.split(" ", 1)[1].strip()
    if not key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Empty API key")
    kind = await tenants.get_key_kind(key)
    if kind != "secret":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Publishable key is read-only; use a secret key for this operation",
        )


def require_admin(
    admin_key: Annotated[str | None, Header(alias="Admin-Key")] = None,
    authorization: str | None = Header(default=None),
) -> None:
    """Guard tenant admin operations. If ADMIN_API_KEY is configured, the
    Admin-Key header (or Authorization: Bearer *** must match; otherwise
    admin is fail-closed (403). There is no "open by default" dev mode.
    Accepting the standard Authorization Bearer form lets Prometheus scrape
    /metrics via its native `authorization` block.
    """
    settings = get_settings()
    if not settings.admin_api_key:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Admin API key not set"
        )
    provided = admin_key
    if not provided and authorization and str(authorization).lower().startswith("bearer "):
        provided = authorization.split(" ", 1)[1].strip()
    if provided != settings.admin_api_key:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Admin key required"
        )