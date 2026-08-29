"""v10.6 — non-destructive tenant-schema migration for live DBs.

A production tenant DB created before v9/v10.6 lacks columns like allowed_groups /
system_prompt / lead_webhook_url / ingest_webhook_url. init_db() must ADD them without
dropping existing rows, so a deploy never breaks on first boot against a stale DB.
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


def test_init_db_migrates_missing_columns_without_data_loss():
    fd, p = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db_url = f"sqlite:///{p}"
    os.environ["DB_URL"] = db_url
    try:
        from app.config import get_settings

        get_settings.cache_clear()

        # Pre-seed a stale-schema DB.
        _make_stale_tenant_db(p)

        from app import db as dbmod
        import asyncio

        async def _work():
            # Force a fresh engine bound to our temp DB.
            dbmod._engine = None  # type: ignore[attr-defined]
            engine = dbmod.get_engine()
            async with engine.begin() as conn:
                await conn.run_sync(dbmod._migrate_tenant_columns)

            # Confirm the legacy row survived migration and new columns exist (before write).
            import sqlite3

            con = sqlite3.connect(p)
            row = con.execute(
                "SELECT tenant_id, name, chunk_count, allowed_groups, ingest_webhook_url "
                "FROM tenants WHERE tenant_id='t_old'"
            ).fetchone()
            con.close()
            assert row is not None, "legacy row was lost during migration"
            tid, name, chunks, allowed, ingest = row
            assert tid == "t_old" and name == "legacy" and chunks == 7
            # New columns added (NULL or '' is acceptable; app treats None as empty).
            assert ingest in (None, "")
            assert allowed in (None, "", "[]")

            # A tenant write that touches the new columns must now succeed end-to-end.
            await dbmod.init_db()
            await dbmod.set_tenant_ingest_webhook("t_old", "https://hooks.example.com/x")
            after = await dbmod.get_tenant("t_old")
            return after

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
