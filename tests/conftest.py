"""Shared pytest fixtures for the RAG service test suite."""
import os
import tempfile
import time

import pytest
from fastapi.testclient import TestClient

# Force a hermetic test environment. We use in-process embedded Qdrant (no network
# server needed) so the suite does not depend on a daemon at localhost:6333, and we
# clear any operator env (QDRANT_URL/REDIS_URL) that may have leaked in from the shell
# so it cannot override these defaults.
os.environ["QDRANT_URL"] = ":memory:"
os.environ.pop("REDIS_URL", None)
os.environ["ADMIN_API_KEY"] = "test-admin-key-for-tests"
os.environ["USE_REAL_EMBEDDER"] = "0"
os.environ["USE_REAL_RERANKER"] = "0"
os.environ["MASTER_ENCRYPTION_KEY"] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
os.environ["WEBHOOK_ALLOW_PRIVATE"] = "1"


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    # get_settings() is lru_cached; clear between tests so env-based fixtures
    # (admin key, quotas, trusted proxies) don't leak stale values across tests.
    # Also reset the process-wide rate limiter so per-IP/per-tenant buckets don't
    # accumulate across tests in the same session. Force in-memory mode for determinism.
    import os
    import asyncio

    from app.config import get_settings
    from app.ratelimit import reset_limiter
    from app.vector_store import reset_client

    # Use a temporary file for the database that gets cleaned up automatically
    db_fd, db_path = tempfile.mkstemp(suffix=".db", prefix="test_rag_")
    os.close(db_fd)
    
    try:
        os.environ["DB_URL"] = f"sqlite:///{db_path}"
        
        # Clear settings cache and reset other global state.
        get_settings.cache_clear()
        reset_client()
        reset_limiter("memory")  # Force in-memory limiter for tests
        
        # Reset the database engine and session maker to avoid cross-test contamination
        import app.db
        app.db._engine = None
        app.db._session_maker = None
        
        # Initialize the database (async function).
        from app.db import init_db
        asyncio.run(init_db())

        yield

    finally:
        # Teardown: remove the temporary database file and clear settings again.
        try:
            import app.db
            engine = app.db._engine
            if engine is not None:
                asyncio.run(engine.dispose())
                app.db._engine = None
                app.db._session_maker = None
            os.unlink(db_path)
        except (FileNotFoundError, PermissionError):
            pass
        except PermissionError:
            pass
        get_settings.cache_clear()
        reset_client()
        reset_limiter("memory")
        # Reset the database engine and session maker
        import app.db
        app.db._engine = None
        app.db._session_maker = None


@pytest.fixture()
def client():
    from app.main import app
    with TestClient(app) as c:
        # Wait for Qdrant to be ready (health endpoint) to avoid flaky connection errors in tests.
        for _ in range(10):
            try:
                if c.get("/health").status_code == 200:
                    break
            except Exception as _conn_err:  # noqa: BLE001, S110 - health may flap during boot; ignore to retry
                pass
            time.sleep(0.5)
        yield c
