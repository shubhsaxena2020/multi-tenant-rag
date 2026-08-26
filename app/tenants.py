"""Tenant registry. SQLite v1 (single-file, zero-ops); interface is Postgres-ready.

Stores tenant_id, name, api_key (hashed for lookup), plan, created_at.
api_key is indexed for O(1) resolution; we store a SHA-256 of the key (not the raw
key) so a DB leak does not immediately compromise tenants.
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
    api_key_prefix: str  # first 8 chars for display
    plan: str
    created_at: datetime


_DB_LOCK = threading.Lock()


def _connect() -> sqlite3.Connection:
    url = get_settings().db_url
    if url.startswith("sqlite:///"):
        path = url[len("sqlite:///"):]
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False)
    else:
        # Postgres-style URLs would swap to psycopg here; v1 is sqlite.
        raise RuntimeError(f"Unsupported db_url for v1: {url}")
    conn.row_factory = sqlite3.Row
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    # CREATE TABLE IF NOT EXISTS is idempotent; run it on every connect so the
    # schema is always present even if the db file was deleted/recreated.
    with _DB_LOCK:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tenants (
                tenant_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                api_key_hash TEXT UNIQUE NOT NULL,
                api_key_prefix TEXT NOT NULL,
                plan TEXT NOT NULL,
                created_at TEXT NOT NULL
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
            "INSERT INTO tenants (tenant_id, name, api_key_hash, api_key_prefix, plan, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (tenant_id, name, _key_hash(api_key), api_key[:8], plan, now),
        )
        conn.commit()
    return TenantRow(
        tenant_id=tenant_id, name=name, api_key_prefix=api_key[:8], plan=plan, created_at=now
    )


def get_tenant_by_key(api_key: str) -> TenantRow | None:
    conn = _connect()
    _init_schema(conn)
    row = conn.execute(
        "SELECT * FROM tenants WHERE api_key_hash = ?", (_key_hash(api_key),)
    ).fetchone()
    if row is None:
        return None
    return TenantRow(
        tenant_id=row["tenant_id"],
        name=row["name"],
        api_key_prefix=row["api_key_prefix"],
        plan=row["plan"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


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
    )


def list_tenants() -> list[TenantRow]:
    conn = _connect()
    _init_schema(conn)
    rows = conn.execute("SELECT * FROM tenants").fetchall()
    return [
        TenantRow(
            tenant_id=r["tenant_id"],
            name=r["name"],
            api_key_prefix=r["api_key_prefix"],
            plan=r["plan"],
            created_at=datetime.fromisoformat(r["created_at"]),
        )
        for r in rows
    ]


def delete_tenant(tenant_id: str) -> bool:
    """Remove a tenant from the registry. Returns True if a row was deleted.
    Callers should also drop the tenant's Qdrant collection for full offboarding."""
    conn = _connect()
    _init_schema(conn)
    cur = conn.execute("DELETE FROM tenants WHERE tenant_id=?", (tenant_id,))
    conn.commit()
    return cur.rowcount > 0
