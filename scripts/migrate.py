#!/usr/bin/env python3
"""Idempotent schema migration for rag-service.

Ensures every table the ORM models declare exists in the configured DB_URL
(including the eval tables - a pre-v6 deployment can be missing eval_sets,
which makes /eval/run return 500). Safe to run repeatedly. Additive column
migrations already run on app startup; this covers missing *tables* for an
existing database that setup.sh's fresh-.env path would never hit.

    python -m scripts.migrate
"""
from __future__ import annotations

import asyncio
import sys


async def _run() -> int:
    # Importing these registers every table on Base.metadata before create_all.
    import app.eval  # noqa: F401
    import app.models  # noqa: F401
    from app.db import init_db

    await init_db()
    print("migrate: schema ensured (create_all - missing tables created, existing untouched)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
