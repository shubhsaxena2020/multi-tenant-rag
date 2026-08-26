"""Tenant registry + API-key management. SQLite v1 (zero-ops); Postgres-ready.

Stores tenant_id, name, plan, created_at, and a per-tenant CHUNK COUNT (for fleet
quotas). API keys live in a separate `tenant_keys` table so a tenant can have multiple
rotatable keys (each hashed; we never store raw keys). The registry row maps tenant_id
→ display only; key resolution is O(1) on the hashed key.

Security (Truto 2026): raw API keys are returned exactly once at creation; only SHA-256
hashes are persisted, so a DB leak does not compromise tenants.
"""
from __future__ import annotations

import hashlib
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from .config import get_settings


def _key_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class TenantRow(BaseModel):
    tenant_id: str
    name: str
    api_key_prefix: str  # first 8 chars of the *active* key, for display
    plan: str
    created_at: datetime

    # runtime-only field, not persisted
    chunk_count: int = 0


_DB_LOCK = threading.Lock()


def _connect() -> sqlite3.Connection:
    url = get_settings().db_url
    if url.startswith("sqlite:///"):
        path = url[len("sqlite:///"):]
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False)
    else:
        raise RuntimeError(f"Unsupported db_url for v1: {url}")
    conn.row_factory = sqlite3.Row
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    with _DB_LOCK:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tenants (
                tenant_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                api_key_prefix TEXT NOT NULL,
                plan TEXT NOT NULL,
                created_at TEXT NOT NULL,
                chunk_count INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tenant_keys (
                key_hash TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                prefix TEXT NOT NULL,
                created_at TEXT NOT NULL,
                revoked INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.commit()


def create_tenant(name: str, tenant_id: str, api_key: str, plan: str) -> TenantRow:
    conn = _connect()
    _init_schema(conn)
    now = datetime.now(UTC).isoformat()
    with _DB_LOCK:
        conn.execute(
            "INSERT INTO tenants (tenant_id, name, api_key_prefix, plan, created_at, chunk_count) "
            "VALUES (?, ?, ?, ?, ?, 0)",
            (tenant_id, name, api_key[:8], plan, now),
        )
        conn.execute(
            "INSERT INTO tenant_keys (key_hash, tenant_id, prefix, created_at, revoked) "
            "VALUES (?, ?, ?, ?, 0)",
            (_key_hash(api_key), tenant_id, api_key[:8], now),
        )
        conn.commit()
    return TenantRow(
        tenant_id=tenant_id, name=name, api_key_prefix=api_key[:8], plan=plan, created_at=now
    )


def add_api_key(tenant_id: str, api_key: str) -> None:
    """Add a secondary/rotated key for an existing tenant (hashed)."""
    conn = _connect()
    _init_schema(conn)
    now = datetime.now(UTC).isoformat()
    with _DB_LOCK:
        conn.execute(
            "INSERT INTO tenant_keys (key_hash, tenant_id, prefix, created_at, revoked) "
            "VALUES (?, ?, ?, ?, 0)",
            (_key_hash(api_key), tenant_id, api_key[:8], now),
        )
        conn.commit()


def revoke_api_key(tenant_id: str, key_prefix: str) -> int:
    """Revoke a key by its prefix. Returns number of keys revoked."""
    conn = _connect()
    _init_schema(conn)
    with _DB_LOCK:
        cur = conn.execute(
            "UPDATE tenant_keys SET revoked=1 WHERE tenant_id=? AND prefix=? AND revoked=0",
            (tenant_id, key_prefix),
        )
        # never allow revoking the last valid key
        valid = conn.execute(
            "SELECT COUNT(*) AS c FROM tenant_keys WHERE tenant_id=? AND revoked=0",
            (tenant_id,),
        ).fetchone()["c"]
        if valid == 0 and cur.rowcount > 0:
            # restore: keep at least one key valid (the most recently created)
            conn.execute(
                "UPDATE tenant_keys SET revoked=0 WHERE rowid = ("
                "SELECT rowid FROM tenant_keys WHERE tenant_id=? AND prefix=? "
                "ORDER BY created_at DESC LIMIT 1)",
                (tenant_id, key_prefix),
            )
            conn.commit()
            return 0
        conn.commit()
        return cur.rowcount


def list_key_prefixes(tenant_id: str) -> list[dict]:
    conn = _connect()
    _init_schema(conn)
    rows = conn.execute(
        "SELECT prefix, created_at, revoked FROM tenant_keys WHERE tenant_id=? ORDER BY created_at DESC",
        (tenant_id,),
    ).fetchall()
    return [{"prefix": r["prefix"], "created_at": r["created_at"], "revoked": bool(r["revoked"])}
            for r in rows]


def get_tenant_by_key(api_key: str) -> TenantRow | None:
    conn = _connect()
    _init_schema(conn)
    key_row = conn.execute(
        "SELECT tenant_id FROM tenant_keys WHERE key_hash = ? AND revoked=0",
        (_key_hash(api_key),),
    ).fetchone()
    if key_row is None:
        return None
    return get_tenant(key_row["tenant_id"])


def get_tenant(tenant_id: str) -> TenantRow | None:
    conn = _connect()
    _init_schema(conn)
    row = conn.execute("SELECT * FROM tenants WHERE tenant_id = ?", (tenant_id,)).fetchone()
    if row is None:
        return None
    return TenantRow(
        tenant_id=row["tenant_id"],
        name=row["name"],
        api_key_prefix=row["api_key_prefix"],
        plan=row["plan"],
        created_at=datetime.fromisoformat(row["created_at"]),
        chunk_count=row["chunk_count"],
    )


def list_tenants() -> list[TenantRow]:
    conn = _connect()
    _init_schema(conn)
    rows = conn.execute("SELECT * FROM tenants").fetchall()
    return [
        TenantRow(
            tenant_id=r["tenant_id"], name=r["name"], api_key_prefix=r["api_key_prefix"],
            plan=r["plan"], created_at=datetime.fromisoformat(r["created_at"]),
            chunk_count=r["chunk_count"],
        )
        for r in rows
    ]


def increment_chunk_count(tenant_id: str, n: int) -> None:
    conn = _connect()
    _init_schema(conn)
    with _DB_LOCK:
        conn.execute(
            "UPDATE tenants SET chunk_count = chunk_count + ? WHERE tenant_id = ?",
            (n, tenant_id),
        )
        conn.commit()


def chunk_count(tenant_id: str) -> int:
    t = get_tenant(tenant_id)
    return t.chunk_count if t else 0


def delete_tenant(tenant_id: str) -> bool:
    """Remove a tenant (keys + registry row). Callers also drop the Qdrant collection."""
    conn = _connect()
    _init_schema(conn)
    with _DB_LOCK:
        conn.execute("DELETE FROM tenant_keys WHERE tenant_id=?", (tenant_id,))
        cur = conn.execute("DELETE FROM tenants WHERE tenant_id=?", (tenant_id,))
        conn.commit()
        return cur.rowcount > 0
