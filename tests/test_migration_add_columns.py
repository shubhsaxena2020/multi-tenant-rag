"""v10.6 / v10.8 — non-destructive schema migration for live DBs.

A production DB created before v9/v10.6 lacks tenant columns (allowed_groups /
system_prompt / lead_webhook_url / ingest_webhook_url / chunk_quota), and a DB created
before v10.8 lacks tenant_keys.expires_at. init_db() must ADD them without dropping
existing rows, so a deploy never breaks on first boot against a stale DB.
"""
import os
import tempfile

import pytest


def _make_stale_tenant_db(path: str) -> None:
    """Create a `tenants` table shaped like an early (pre-v9) schema (no new columns)."""
    import sqlite3

    con = sqlite3.connect(path)
    con.execute(
        """CREATE TABLE tenants (
            tenant_id TEXT PRIMARY KEY,
            name TEXT,
            api_key_prefix TEXT,
            plan TEXT,
            created_at TEXT,
            chunk_count INTEGER DEFAULT 0
        )"""
    )
    con.execute(
        "INSERT INTO tenants (tenant_id, name, api_key_prefix, plan, created_at, chunk_count) "
        "VALUES ('t_old','legacy','rk_old','standard','2024-01-01',7)"
    )
    con.commit()
    con.close()


def _make_stale_tenant_keys_db(path: str) -> None:
    """Create pre-v10.8 `tenant_keys` (no expires_at) plus a tenant that owns a key."""
    import sqlite3

    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE tenants (tenant_id TEXT PRIMARY KEY, name TEXT, api_key_prefix TEXT, "
        "plan TEXT, created_at TEXT, chunk_count INTEGER DEFAULT 0)"
    )
    con.execute(
        "INSERT INTO tenants (tenant_id, name, api_key_prefix, plan, created_at, chunk_count) "
        "VALUES ('t_keys','legacy','rk_keys','standard','2024-01-01',0)"
    )
    con.execute(
        """CREATE TABLE tenant_keys (
            key_hash TEXT PRIMARY KEY,
            tenant_id TEXT,
            prefix TEXT,
            created_at TEXT,
            revoked INTEGER DEFAULT 0,
            kind TEXT DEFAULT 'secret'
        )"""
    )
    con.execute(
        "INSERT INTO tenant_keys (key_hash, tenant_id, prefix, created_at, revoked, kind) "
        "VALUES ('h1','t_keys','rk_keys','2024-01-01',0,'secret')"
    )
    con.commit()
    con.close()


def test_init_db_migrates_missing_columns_without_data_loss():
    fd, p = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db_url = f"sqlite:///{p}"
    os.environ["DB_URL"] = db_url
    try:
        from app.config import get_settings

        get_settings.cache_clear()

        _make_stale_tenant_db(p)

        from app import db as dbmod
        import asyncio
        import sqlite3

        async def _work():
            dbmod._engine = None  # type: ignore[attr-defined]
            engine = dbmod.get_engine()
            async with engine.begin() as conn:
                await conn.run_sync(dbmod._migrate_tenant_columns)

            con = sqlite3.connect(p)
            row = con.execute(
                "SELECT tenant_id, name, chunk_count, allowed_groups, ingest_webhook_url "
                "FROM tenants WHERE tenant_id='t_old'"
            ).fetchone()
            con.close()
            assert row is not None, "legacy row was lost during migration"
            tid, name, chunks, allowed, ingest = row
            assert tid == "t_old" and name == "legacy" and chunks == 7
            assert ingest in (None, "")
            assert allowed in (None, "", "[]")

            await dbmod.init_db()
            await dbmod.set_tenant_ingest_webhook("t_old", "https://hooks.example.com/x")
            return await dbmod.get_tenant("t_old")

        after = asyncio.run(_work())
        assert after["ingest_webhook_url"] == "https://hooks.example.com/x"
        assert after["lead_webhook_url"] == ""
        assert after["chunk_count"] == 7
    finally:
        os.environ.pop("DB_URL", None)
        from app.config import get_settings as _gs

        _gs.cache_clear()
        try:
            os.unlink(p)
        except OSError:
            pass


def test_init_db_migrates_tenant_keys_expires_at():
    fd, p = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db_url = f"sqlite:///{p}"
    os.environ["DB_URL"] = db_url
    try:
        from app.config import get_settings

        get_settings.cache_clear()
        _make_stale_tenant_keys_db(p)
        from app import db as dbmod
        import asyncio
        from datetime import datetime, timedelta, timezone

        async def _work():
            dbmod._engine = None  # type: ignore[attr-defined]
            engine = dbmod.get_engine()
            async with engine.begin() as conn:
                await conn.run_sync(dbmod._migrate_tenant_columns)
                await conn.run_sync(dbmod._migrate_tenant_keys_columns)
            # Seed a legacy key with NO expiry (simulating a pre-v10.8 key) via the normal
            # path so its hash is correct, then an expired key directly with a past expiry.
            from datetime import datetime, timedelta, timezone

            await dbmod.add_api_key("t_keys", "rk_legacy_xyzLEGACY")  # no expiry
            past = datetime.now(timezone.utc) - timedelta(days=1)
            # Manually set a past expiry on a second key (add_api_key only takes a datetime).
            await dbmod.add_api_key("t_keys", "rk_exp_xyzEXPIRED", expires_at=past)
            return (
                await dbmod.get_tenant_by_key("rk_legacy_xyzLEGACY"),
                await dbmod.get_tenant_by_key("rk_exp_xyzEXPIRED"),
            )

        legacy, expired = asyncio.run(_work())
        # Expired key is rejected by the migration-aware resolution filter.
        assert expired is None
        # Legacy key (no expiry) still resolves.
        assert legacy is not None
    finally:
        os.environ.pop("DB_URL", None)
        from app.config import get_settings as _gs

        _gs.cache_clear()
        try:
            os.unlink(p)
        except OSError:
            pass
