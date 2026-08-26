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


def get_tenant_from_header(
    authorization: Annotated[str | None, Header()] = None,
) -> tenants.TenantRow:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header",
        )
    key = authorization.split(" ", 1)[1].strip()
    if not key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Empty API key")
    tenant = tenants.get_tenant_by_key(key)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
    return tenant


def require_admin(admin_key: Annotated[str | None, Header(alias="Admin-Key")] = None) -> None:
    """Guard tenant admin operations. If ADMIN_API_KEY is configured, the
    Admin-Key header must match; otherwise (dev) admin is open."""
    settings = get_settings()
    if not settings.admin_api_key:
        return
    if admin_key != settings.admin_api_key:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Admin key required"
        )
