"""Tenant registry + API-key management. PostgreSQL (fleet) + SQLite (dev/CI) via async SQLAlchemy.
Stores tenant_id, name, plan, created_at, and a per-tenant CHUNK COUNT (for fleet quotas).
API keys live in a separate `tenant_keys` table so a tenant can have multiple rotatable keys
(each hashed; we never store raw keys). The registry row maps tenant_id → display only;
key resolution is O(1) on the hashed key.

Security (Truto 2026): raw API keys are returned exactly once at creation; only SHA-256
hashes are persisted, so a DB leak does not compromise tenants.
"""
from __future__ import annotations

import asyncio
from typing import Any

from .db import (
    add_api_key as _add_api_key,
)
from .db import (
    chunk_count as _chunk_count,
)
from .db import (
    create_tenant as _create_tenant,
)
from .db import (
    delete_tenant as _delete_tenant,
)
from .db import (
    get_tenant as _get_tenant,
)
from .db import (
    get_tenant_by_key as _get_tenant_by_key,
)
from .db import (
    increment_chunk_count as _increment_chunk_count,
)
from .db import (
    list_key_prefixes as _list_key_prefixes,
)
from .db import (
    list_tenants as _list_tenants,
)
from .db import (
    revoke_api_key as _revoke_api_key,
)
from .db import (
    set_key_expiry as _set_key_expiry,
)
from .models import TenantRow


async def create_tenant(
    name: str,
    tenant_id: str,
    api_key: str,
    plan: str,
    allowed_groups: list[str] | None = None,
) -> TenantRow:
    data = await _create_tenant(name, tenant_id, api_key, plan, allowed_groups)
    return TenantRow(**data)


async def add_api_key(tenant_id: str, api_key: str, kind: str = "secret", expires_at=None) -> None:
    await _add_api_key(tenant_id, api_key, kind=kind, expires_at=expires_at)


async def set_key_expiry(tenant_id: str, key_prefix: str, expires_at) -> int:
    return await _set_key_expiry(tenant_id, key_prefix, expires_at)


async def revoke_api_key(tenant_id: str, key_prefix: str) -> int:
    return await _revoke_api_key(tenant_id, key_prefix)


async def list_key_prefixes(tenant_id: str) -> list[dict[str, Any]]:
    return await _list_key_prefixes(tenant_id)


async def get_tenant_by_key(api_key: str) -> TenantRow | None:
    data = await _get_tenant_by_key(api_key)
    if data is None:
        return None
    return TenantRow(**data)


async def get_key_kind(api_key: str) -> str | None:
    """P1 #9: return the tier of a raw API key ('secret' | 'publishable' | None if unknown)."""
    from .db import get_key_kind as _get_key_kind

    return await _get_key_kind(api_key)


async def get_tenant(tenant_id: str) -> TenantRow | None:
    data = await _get_tenant(tenant_id)
    if data is None:
        return None
    return TenantRow(**data)


async def list_tenants() -> list[TenantRow]:
    data = await _list_tenants()
    return [TenantRow(**d) for d in data]


async def increment_chunk_count(tenant_id: str, n: int) -> None:
    await _increment_chunk_count(tenant_id, n)


async def chunk_count_async(tenant_id: str) -> int:
    return await _chunk_count(tenant_id)


def chunk_count(tenant_id: str) -> int:
    """Sync wrapper for backward compatibility (tests)."""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(_chunk_count(tenant_id))


async def delete_tenant(tenant_id: str) -> bool:
    return await _delete_tenant(tenant_id)
