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


class TenantKey(Base):
    __tablename__ = "tenant_keys"

    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


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
    """Create tables if they don't exist. Safe to call multiple times."""
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency for getting a DB session."""
    async with get_session_maker()() as session:
        yield session


def _key_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


async def create_tenant(
    name: str,
    tenant_id: str,
    api_key: str,
    plan: str,
    allowed_groups: list[str] | None = None,
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
        }


async def add_api_key(tenant_id: str, api_key: str, session: AsyncSession | None = None) -> None:
    """Add a secondary/rotated key for an existing tenant (hashed)."""
    now = datetime.now(UTC)
    async with (session or get_session_maker())() as s:
        key = TenantKey(
            key_hash=_key_hash(api_key),
            tenant_id=tenant_id,
            prefix=api_key[:8],
            created_at=now,
            revoked=False,
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
            select(TenantKey.prefix, TenantKey.created_at, TenantKey.revoked)
            .where(TenantKey.tenant_id == tenant_id)
            .order_by(TenantKey.created_at.desc())
        )
        result = await s.execute(stmt)
        return [
            {"prefix": r.prefix, "created_at": r.created_at.isoformat(), "revoked": r.revoked}
            for r in result.all()
        ]


async def get_tenant_by_key(api_key: str, session: AsyncSession | None = None) -> dict | None:
    """Resolve tenant by raw API key (hash lookup)."""
    async with (session or get_session_maker())() as s:
        stmt = select(TenantKey.tenant_id).where(
            TenantKey.key_hash == _key_hash(api_key),
            TenantKey.revoked == False,
        )
        result = await s.execute(stmt)
        tenant_id = result.scalar_one_or_none()
        if tenant_id is None:
            return None
        return await get_tenant(tenant_id, s)


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
