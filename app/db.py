"""PostgreSQL database layer with async SQLAlchemy 2.0 + asyncpg.
Supports both SQLite (dev/CI) and PostgreSQL (production fleet).
All operations are async for horizontal scaling compatibility.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Integer,
    String,
    Text,
    delete,
    func,
    or_,
    select,
    update,
)
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .config import get_settings


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"

    tenant_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    api_key_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    plan: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    allowed_groups: Mapped[str] = mapped_column(Text, default='["*"]', nullable=False)
    # Per-tenant widget branding (issue #23): JSON blob {logo_url, header_title, accent, ...}.
    # Nullable text, default '{}'. Applied by the embeddable widget via CSS custom properties.
    branding: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    # PHASE D (#35): per-tenant custom system prompt / persona. Operator-trusted config (NOT
    # end-user input), so it is the system's own instruction surface, not subject to the
    # user-input injection filtering owned by the parallel P0/P1 security session. Empty = default.
    system_prompt: Mapped[str] = mapped_column(Text, default="", nullable=False)


class TenantKey(Base):
    __tablename__ = "tenant_keys"

    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # P1 #9: key tier. "secret" = full-power key (rk_*, can ingest/admin/rotate);
    # "publishable" = read-only key (pk_*) safe to embed client-side in the widget.
    # A publishable key resolves to the SAME tenant_id (isolation is unchanged) but is
    # scope-locked to query endpoints by app/auth.py:require_secret_key.
    kind: Mapped[str] = mapped_column(String(16), default="secret", server_default="secret", nullable=False)
    # v10.8: optional UTC expiry. NULL = never expires; expired keys are rejected at auth
    # time (see require_secret_key / list_keys). Was referenced by add_api_key/require_secret_key
    # but the column was missing from the model -> every secret-key-guarded endpoint crashed.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Job(Base):
    __tablename__ = "jobs"

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    progress: Mapped[float] = mapped_column(default=0.0, nullable=False)
    total_chunks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    done_chunks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_doc_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)


class DocumentRegistry(Base):
    """Per-tenant logical-document registry (GitHub issue #4).

    Maps a stable, tenant-scoped `doc_key` (derived from source URL or title+type) to the
    current `doc_id` in Qdrant, plus the `content_hash` of what is indexed. Re-ingesting the
    same source reuses (idempotent) or replaces (content changed) the prior chunks instead of
    creating fresh duplicates. Created as a new table via `create_all`; no ALTER migration.
    """

    __tablename__ = "document_registry"

    tenant_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    doc_key: Mapped[str] = mapped_column(String(512), primary_key=True)
    doc_id: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    content_type: Mapped[str] = mapped_column(String(32), nullable=False, default="text")
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ConversationSession(Base):
    """Durable multi-turn conversation history (GitHub issue: PHASE C session persistence).

    Backs the embeddable widget's multi-turn memory so a conversation survives process
    restarts and is shared across replicas that use the same database. `turns` is a JSON list
    of {role, text} objects (most-recent last). Tenant-scoped for isolation; `session_id` is
    unique per tenant.
    """

    __tablename__ = "conversation_sessions"

    tenant_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    turns: Mapped[str] = mapped_column(Text, nullable=False, default="[]")  # JSON-encoded list
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


_engine: AsyncEngine | None = None
_session_maker: async_sessionmaker[AsyncSession] | None = None


def _to_async_url(url: str) -> str:
    """Convert sync DB URL to async variant."""
    if url.startswith("sqlite:///"):
        return "sqlite+aiosqlite:///" + url[len("sqlite:///"):]
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://")
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://")
    return url


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        async_url = _to_async_url(settings.db_url)
        _engine = create_async_engine(
            async_url,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
            echo=False,
        )
    return _engine


def get_session_maker() -> async_sessionmaker[AsyncSession]:
    global _session_maker
    if _session_maker is None:
        _session_maker = async_sessionmaker(
            get_engine(), class_=AsyncSession, expire_on_commit=False
        )
    return _session_maker


async def init_db() -> None:
    """Create tables if they don't exist. Safe to call multiple times.

    Also performs non-destructive online migrations (ADD COLUMN IF NOT EXISTS) so existing
    production tables gain new columns without a DROP/DATA LOSS. We never DROP or ALTER-type
    in a way that could lose data (per the explicit human-checkpoint migration rule).

    See GitHub issue #2: `create_all` only creates missing tables, it does not add columns to
    tables that already exist, so new model columns must be migrated here explicitly.
    """
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Idempotent additive column migrations (issue #2). Each entry is (table, column,
        # type). Skipped automatically if the column already exists on the live table.
        # All are nullable + have server defaults, so ADD COLUMN is safe on SQLite + Postgres.
        migrations = [
            ("tenant_keys", "kind", "VARCHAR(16)"),
            ("tenant_keys", "expires_at", "TIMESTAMP WITH TIME ZONE"),
            ("tenants", "chunk_quota", "INTEGER"),
            ("tenants", "branding", "TEXT"),
            ("tenants", "system_prompt", "TEXT"),
        ]
        await _run_add_column_migrations(conn, migrations)


async def _run_add_column_migrations(conn, migrations: list[tuple[str, str, str]]) -> None:
    """Add any missing columns from `migrations` without erroring if they already exist.

    Uses dialect-aware introspection (PRAGMA for SQLite, information_schema for Postgres)
    so a genuine failure is never swallowed by a blanket try/except.
    """
    dialect = conn.sync_engine.dialect.name
    for table, column, col_type in migrations:
        exists = await _column_exists(conn, dialect, table, column)
        if exists:
            continue
        try:
            await conn.exec_driver_sql(f'ALTER TABLE {table} ADD COLUMN {column} {col_type}')
        except Exception:
            # Best-effort: if the column appeared between check and add (race) or the dialect
            # rejected it for a benign reason, do not take the app down — but log loudly.
            import logging
            logging.getLogger("rag.db").warning(
                "migration_add_column_failed", extra={"table": table, "column": column}
            )


async def _column_exists(conn, dialect: str, table: str, column: str) -> bool:
    """Return True if `column` already exists on `table` for the active dialect."""
    if dialect == "sqlite":
        rows = (await conn.exec_driver_sql(f"PRAGMA table_info({table})")).fetchall()
        return any(r[1] == column for r in rows)
    # Postgres (and most others): information_schema.columns
    row = await conn.exec_driver_sql(
        "SELECT 1 FROM information_schema.columns WHERE table_name = :t AND column_name = :c",
        {"t": table, "c": column},
    )
    return row.first() is not None


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency for getting a DB session."""
    async with get_session_maker()() as session:
        yield session


def _key_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _parse_json(value: str | None, default: Any) -> Any:
    """Parse a JSON text column, falling back to `default` on missing/corrupt data."""
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


async def create_tenant(
    name: str,
    tenant_id: str,
    api_key: str,
    plan: str,
    allowed_groups: list[str] | None = None,
    branding: dict | None = None,
    system_prompt: str = "",
    session: AsyncSession | None = None,
) -> dict:
    """Create a new tenant with initial API key. Returns tenant dict."""
    now = datetime.now(UTC)
    groups = allowed_groups if allowed_groups is not None else ["*"]
    async with (session or get_session_maker())() as s:
        tenant = Tenant(
            tenant_id=tenant_id,
            name=name,
            api_key_prefix=api_key[:8],
            plan=plan,
            created_at=now,
            chunk_count=0,
            allowed_groups=json.dumps(groups),
            branding=json.dumps(branding or {}),
            system_prompt=system_prompt or "",
        )
        s.add(tenant)
        key = TenantKey(
            key_hash=_key_hash(api_key),
            tenant_id=tenant_id,
            prefix=api_key[:8],
            created_at=now,
            revoked=False,
        )
        s.add(key)
        await s.commit()
        await s.refresh(tenant)
        return {
            "tenant_id": tenant.tenant_id,
            "name": tenant.name,
            "api_key": api_key,
            "plan": tenant.plan,
            "created_at": tenant.created_at,
            "chunk_count": tenant.chunk_count,
            "allowed_groups": groups,
            "branding": _parse_json(tenant.branding, {}),
            "system_prompt": tenant.system_prompt or "",
        }


async def add_api_key(tenant_id: str, api_key: str, kind: str = "secret", expires_at: datetime | None = None, session: AsyncSession | None = None) -> None:
    """Add a secondary/rotated key for an existing tenant (hashed).

    P1 #9: `kind` = "secret" (full power, rk_*) or "publishable" (read-only, pk_*).
    v10.8: `expires_at` = optional UTC expiry. NULL = never expires; expired keys are
    rejected at resolution time (see get_tenant_by_key / get_key_kind).
    """
    now = datetime.now(UTC)
    async with (session or get_session_maker())() as s:
        key = TenantKey(
            key_hash=_key_hash(api_key),
            tenant_id=tenant_id,
            prefix=api_key[:8],
            created_at=now,
            revoked=False,
            kind=kind,
            expires_at=expires_at,
        )
        s.add(key)
        await s.commit()


async def revoke_api_key(tenant_id: str, key_prefix: str, session: AsyncSession | None = None) -> int:
    """Revoke a key by its prefix. Returns number of keys revoked."""
    async with (session or get_session_maker())() as s:
        # Find the key to revoke
        stmt = select(TenantKey).where(
            TenantKey.tenant_id == tenant_id,
            TenantKey.prefix == key_prefix,
            TenantKey.revoked == False,
        )
        result = await s.execute(stmt)
        key = result.scalar_one_or_none()
        if key is None:
            return 0
        key.revoked = True
        # Ensure at least one valid key remains
        valid_count = await s.scalar(
            select(func.count(TenantKey.key_hash)).where(
                TenantKey.tenant_id == tenant_id,
                TenantKey.revoked == False,
            )
        )
        if valid_count == 0:
            # Restore the most recent key
            stmt2 = (
                select(TenantKey)
                .where(TenantKey.tenant_id == tenant_id, TenantKey.prefix == key_prefix)
                .order_by(TenantKey.created_at.desc())
                .limit(1)
            )
            result2 = await s.execute(stmt2)
            last_key = result2.scalar_one_or_none()
            if last_key:
                last_key.revoked = False
                await s.commit()
                return 0
        await s.commit()
        return 1


async def list_key_prefixes(tenant_id: str, session: AsyncSession | None = None) -> list[dict]:
    async with (session or get_session_maker())() as s:
        stmt = (
            select(TenantKey.prefix, TenantKey.created_at, TenantKey.revoked, TenantKey.kind, TenantKey.expires_at)
            .where(TenantKey.tenant_id == tenant_id)
            .order_by(TenantKey.created_at.desc())
        )
        result = await s.execute(stmt)
        return [
            {"prefix": r.prefix, "created_at": r.created_at.isoformat(),
             "revoked": r.revoked, "kind": r.kind,
             "expires_at": r.expires_at.isoformat() if r.expires_at is not None else None}
            for r in result.all()
        ]


async def set_key_expiry(tenant_id: str, key_prefix: str, expires_at: datetime | None,
                         session: AsyncSession | None = None) -> int:
    """v10.8: set (or clear, with expires_at=None) the expiry for a key by prefix.

    Returns number of keys updated. Expiry is validated at resolution time by
    get_tenant_by_key / get_key_kind (expired keys are rejected like revoked keys).
    """
    async with (session or get_session_maker())() as s:
        n = await s.execute(
            update(TenantKey)
            .where(TenantKey.tenant_id == tenant_id, TenantKey.prefix == key_prefix,
                   TenantKey.revoked == False)
            .values(expires_at=expires_at)
        )
        await s.commit()
        return n.rowcount if hasattr(n, "rowcount") else 0


async def get_tenant_by_key(api_key: str, session: AsyncSession | None = None) -> dict | None:
    """Resolve tenant by raw API key (hash lookup).

    v10.8: expired keys are rejected (treated like revoked -> None -> 401 upstream).
    """
    now = datetime.now(UTC)
    async with (session or get_session_maker())() as s:
        stmt = select(TenantKey.tenant_id, TenantKey.kind).where(
            TenantKey.key_hash == _key_hash(api_key),
            TenantKey.revoked == False,
            or_(TenantKey.expires_at == None, TenantKey.expires_at > now),
        )
        result = await s.execute(stmt)
        row = result.first()
        if row is None:
            return None
        tenant_id, _kind = row
        return await get_tenant(tenant_id, s)


async def get_key_kind(api_key: str, session: AsyncSession | None = None) -> str | None:
    """P1 #9: return key tier ('secret' | 'publishable') or None if unknown/revoked/expired."""
    now = datetime.now(UTC)
    async with (session or get_session_maker())() as s:
        stmt = select(TenantKey.kind).where(
            TenantKey.key_hash == _key_hash(api_key),
            TenantKey.revoked == False,
            or_(TenantKey.expires_at == None, TenantKey.expires_at > now),
        )
        result = await s.execute(stmt)
        return result.scalar_one_or_none()


async def get_tenant(tenant_id: str, session: AsyncSession | None = None) -> dict | None:
    if session is None:
        async with get_session_maker()() as s:
            stmt = select(Tenant).where(Tenant.tenant_id == tenant_id)
            result = await s.execute(stmt)
            t = result.scalar_one_or_none()
            if t is None:
                return None
            try:
                allowed_groups = json.loads(t.allowed_groups)
            except Exception:
                allowed_groups = ["*"]
            return {
                "tenant_id": t.tenant_id,
                "name": t.name,
                "api_key": t.api_key_prefix,
                "plan": t.plan,
                "created_at": t.created_at,
                "chunk_count": t.chunk_count,
                "allowed_groups": allowed_groups,
                "branding": _parse_json(t.branding, {}),
                "system_prompt": t.system_prompt or "",
            }
    else:
        async with session as s:
            stmt = select(Tenant).where(Tenant.tenant_id == tenant_id)
            result = await s.execute(stmt)
            t = result.scalar_one_or_none()
            if t is None:
                return None
            try:
                allowed_groups = json.loads(t.allowed_groups)
            except Exception:
                allowed_groups = ["*"]
            return {
                "tenant_id": t.tenant_id,
                "name": t.name,
                "api_key": t.api_key_prefix,
                "plan": t.plan,
                "created_at": t.created_at,
                "chunk_count": t.chunk_count,
                "allowed_groups": allowed_groups,
                "branding": _parse_json(t.branding, {}),
                "system_prompt": t.system_prompt or "",
            }


async def list_tenants(session: AsyncSession | None = None) -> list[dict]:
    async with (session or get_session_maker())() as s:
        stmt = select(Tenant)
        result = await s.execute(stmt)
        return [
            {
                "tenant_id": t.tenant_id,
                "name": t.name,
                "api_key_prefix": t.api_key_prefix,
                "plan": t.plan,
                "created_at": t.created_at,
                "chunk_count": t.chunk_count,
            }
            for t in result.scalars().all()
        ]


async def increment_chunk_count(tenant_id: str, n: int, session: AsyncSession | None = None) -> None:
    async with (session or get_session_maker())() as s:
        stmt = (
            update(Tenant)
            .where(Tenant.tenant_id == tenant_id)
            .values(chunk_count=Tenant.chunk_count + n)
        )
        await s.execute(stmt)
        await s.commit()


async def chunk_count(tenant_id: str, session: AsyncSession | None = None) -> int:
    t = await get_tenant(tenant_id, session)
    return t["chunk_count"] if t else 0


async def decrement_chunk_count(tenant_id: str, n: int, session: AsyncSession | None = None) -> None:
    """Decrease a tenant's chunk counter (e.g. when replacing a document's stale chunks).

    Clamped at 0 so a counter can never go negative (defensive against double-deletes).
    """

    async with (session or get_session_maker())() as s:
        stmt = (
            update(Tenant)
            .where(Tenant.tenant_id == tenant_id)
            .values(chunk_count=func.max(Tenant.chunk_count - n, 0))
        )
        await s.execute(stmt)
        await s.commit()


async def set_tenant_branding(tenant_id: str, branding: dict, session: AsyncSession | None = None) -> bool:
    """Persist a tenant's sanitized branding JSON blob. Returns True if the tenant exists."""
    async with (session or get_session_maker())() as s:
        stmt = (
            update(Tenant)
            .where(Tenant.tenant_id == tenant_id)
            .values(branding=json.dumps(branding or {}))
        )
        result = await s.execute(stmt)
        await s.commit()
        return (result.rowcount or 0) > 0


async def set_tenant_system_prompt(tenant_id: str, system_prompt: str, session: AsyncSession | None = None) -> bool:
    """PHASE D (#35): persist a tenant's persona/system prompt. Operator-trusted config (NOT
    end-user input) — no injection filtering applied here; only operators set it via the
    admin-gated endpoint. Empty string clears the persona. Returns True if the tenant exists."""
    async with (session or get_session_maker())() as s:
        stmt = (
            update(Tenant)
            .where(Tenant.tenant_id == tenant_id)
            .values(system_prompt=system_prompt or "")
        )
        result = await s.execute(stmt)
        await s.commit()
        return (result.rowcount or 0) > 0


# ---- Document registry (GitHub issue #4: idempotent + replace-on-change re-ingestion) ----


async def get_registry_entry(tenant_id: str, doc_key: str, session: AsyncSession | None = None) -> dict | None:
    """Return the current registry row for a (tenant, doc_key) or None if unknown."""

    async with (session or get_session_maker())() as s:
        stmt = select(DocumentRegistry).where(
            DocumentRegistry.tenant_id == tenant_id,
            DocumentRegistry.doc_key == doc_key,
        )
        result = await s.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            return None
        return {
            "tenant_id": row.tenant_id,
            "doc_key": row.doc_key,
            "doc_id": row.doc_id,
            "title": row.title,
            "content_type": row.content_type,
            "source_url": row.source_url,
            "content_hash": row.content_hash,
            "chunk_count": row.chunk_count,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }


async def list_documents(
    tenant_id: str, limit: int = 200, offset: int = 0, session: AsyncSession | None = None
) -> list[dict]:
    """Catalog: every document currently indexed for `tenant_id` (issue #7/#12).

    Backed by `document_registry` (issue #4), which is tenant-scoped by construction, so a
    tenant only ever sees their own docs. Ordered most-recently-updated first. `offset`
    enables paging through large catalogs.
    """
    async with (session or get_session_maker())() as s:
        stmt = (
            select(DocumentRegistry)
            .where(DocumentRegistry.tenant_id == tenant_id)
            .order_by(DocumentRegistry.updated_at.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = (await s.execute(stmt)).scalars().all()
        return [
            {
                "doc_id": r.doc_id,
                "doc_key": r.doc_key,
                "title": r.title,
                "content_type": r.content_type,
                "chunk_count": r.chunk_count,
                "source_url": r.source_url,
                "content_hash": r.content_hash,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ]


async def count_documents(tenant_id: str, session: AsyncSession | None = None) -> int:
    """True number of documents indexed for `tenant_id` (independent of paging). Issue #12."""
    async with (session or get_session_maker())() as s:
        return int(
            await s.scalar(
                select(func.count(DocumentRegistry.doc_id)).where(
                    DocumentRegistry.tenant_id == tenant_id
                )
            )
            or 0
        )


async def upsert_registry_entry(
    tenant_id: str,
    doc_key: str,
    doc_id: str,
    *,
    title: str,
    content_type: str,
    source_url: str | None,
    content_hash: str,
    chunk_count: int,
    session: AsyncSession | None = None,
) -> None:
    """Insert or update the registry row for a (tenant, doc_key).

    On update, the existing `doc_id`/`content_hash`/counts are overwritten with the new
    version so the row always points at the currently-indexed document.
    """

    now = datetime.now(UTC)
    async with (session or get_session_maker())() as s:
        stmt = select(DocumentRegistry).where(
            DocumentRegistry.tenant_id == tenant_id,
            DocumentRegistry.doc_key == doc_key,
        )
        existing = (await s.execute(stmt)).scalar_one_or_none()
        if existing is None:
            s.add(
                DocumentRegistry(
                    tenant_id=tenant_id,
                    doc_key=doc_key,
                    doc_id=doc_id,
                    title=title,
                    content_type=content_type,
                    source_url=source_url,
                    content_hash=content_hash,
                    chunk_count=chunk_count,
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            existing.doc_id = doc_id
            existing.title = title
            existing.content_type = content_type
            existing.source_url = source_url
            existing.content_hash = content_hash
            existing.chunk_count = chunk_count
            existing.updated_at = now
        await s.commit()


async def delete_registry_by_doc_id(tenant_id: str, doc_id: str, session: AsyncSession | None = None) -> None:
    """Remove any registry row that points at `doc_id`. Used when the prior version is deleted."""

    async with (session or get_session_maker())() as s:
        await s.execute(
            delete(DocumentRegistry).where(
                DocumentRegistry.tenant_id == tenant_id,
                DocumentRegistry.doc_id == doc_id,
            )
        )
        await s.commit()


async def delete_tenant(tenant_id: str, session: AsyncSession | None = None) -> bool:
    """Remove a tenant (keys + registry row). Callers also drop the Qdrant collection."""
    async with (session or get_session_maker())() as s:
        await s.execute(delete(TenantKey).where(TenantKey.tenant_id == tenant_id))
        result = await s.execute(delete(Tenant).where(Tenant.tenant_id == tenant_id))
        await s.commit()
        return result.rowcount > 0


# Job store operations


async def create_job(
    tenant_id: str, kind: str, title: str, session: AsyncSession | None = None
) -> str:
    import uuid

    job_id = f"job_{uuid.uuid4().hex[:16]}"
    now = datetime.now(UTC)
    async with (session or get_session_maker())() as s:
        job = Job(
            job_id=job_id,
            tenant_id=tenant_id,
            kind=kind,
            status="pending",
            progress=0.0,
            total_chunks=0,
            done_chunks=0,
            created_at=now,
            updated_at=now,
            title=title,
        )
        s.add(job)
        await s.commit()
    return job_id


async def update_job(
    job_id: str,
    *,
    status: str | None = None,
    progress: float | None = None,
    done_chunks: int | None = None,
    total_chunks: int | None = None,
    error: str | None = None,
    result_doc_id: str | None = None,
    session: AsyncSession | None = None,
) -> None:
    sets = {}
    if status is not None:
        sets["status"] = status
    if progress is not None:
        sets["progress"] = progress
    if done_chunks is not None:
        sets["done_chunks"] = done_chunks
    if total_chunks is not None:
        sets["total_chunks"] = total_chunks
    if error is not None:
        sets["error"] = error
    if result_doc_id is not None:
        sets["result_doc_id"] = result_doc_id
    if not sets:
        return
    sets["updated_at"] = datetime.now(UTC)

    async with (session or get_session_maker())() as s:
        stmt = update(Job).where(Job.job_id == job_id).values(**sets)
        await s.execute(stmt)
        await s.commit()


async def get_job(job_id: str, tenant_id: str, session: AsyncSession | None = None) -> dict | None:
    async with (session or get_session_maker())() as s:
        stmt = select(Job).where(Job.job_id == job_id, Job.tenant_id == tenant_id)
        result = await s.execute(stmt)
        job = result.scalar_one_or_none()
        if job is None:
            return None
        return {
            "job_id": job.job_id,
            "tenant_id": job.tenant_id,
            "kind": job.kind,
            "status": job.status,
            "progress": job.progress,
            "total_chunks": job.total_chunks,
            "done_chunks": job.done_chunks,
            "error": job.error,
            "result_doc_id": job.result_doc_id,
            "created_at": job.created_at.isoformat(),
            "updated_at": job.updated_at.isoformat(),
            "title": job.title,
        }


async def list_jobs(tenant_id: str, limit: int = 50, session: AsyncSession | None = None) -> list[dict]:
    async with (session or get_session_maker())() as s:
        stmt = (
            select(Job)
            .where(Job.tenant_id == tenant_id)
            .order_by(Job.created_at.desc())
            .limit(limit)
        )
        result = await s.execute(stmt)
        return [
            {
                "job_id": j.job_id,
                "tenant_id": j.tenant_id,
                "kind": j.kind,
                "status": j.status,
                "progress": j.progress,
                "total_chunks": j.total_chunks,
                "done_chunks": j.done_chunks,
                "error": j.error,
                "result_doc_id": j.result_doc_id,
                "created_at": j.created_at.isoformat(),
                "updated_at": j.updated_at.isoformat(),
                "title": j.title,
            }
            for j in result.scalars().all()
        ]


async def delete_job(job_id: str, tenant_id: str, session: AsyncSession | None = None) -> bool:
    async with (session or get_session_maker())() as s:
        result = await s.execute(
            delete(Job).where(Job.job_id == job_id, Job.tenant_id == tenant_id)
        )
        await s.commit()
        return result.rowcount > 0


async def requeue_orphaned_jobs(session: AsyncSession | None = None) -> int:
    """v9-5: durable job orchestration / crash recovery.

    A job left in `running` when a worker died (OOM, deploy, crash) would otherwise be
    stuck forever. On startup we reset orphaned `running` jobs back to `pending` so a
    worker can pick them up. Returns the number of recovered jobs.

    NOTE: the jobs table is already the durable source of truth (SQLite/Postgres). For
    horizontal scale with N workers, front this with an at-least-once queue (Cloud Tasks /
    RQ / Celery) that calls the existing job runner; this recovery handles the single-replica
    VPS case where the in-memory queue is lost on restart."""
    async with (session or get_session_maker())() as s:
        now = datetime.now(UTC)
        stmt = (
            update(Job)
            .where(Job.status == "running")
            .values(status="pending", progress=0.0, updated_at=now)
        )
        result = await s.execute(stmt)
        await s.commit()
        return result.rowcount
