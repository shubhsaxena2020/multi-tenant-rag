"""v9-5: Job orchestration basic verification.

Tests for job status tracking and DB operations using existing app.db primitives.
"""

import pytest
import os
from app.config import get_settings
from app.jobs import enqueue_job, get_job_status, requeue_orphaned_jobs
from app.db import init_db, create_job, get_job as _db_get_job, update_job, requeue_orphaned_jobs as _requeue_orphaned_jobs_db, Job
from app.db import get_session_maker

# Skip Redis tests if REDIS_URL not configured
REDIS_URL = os.environ.get("REDIS_URL")


@pytest.fixture(scope="module")
def module_fixture():
    import asyncio
    asyncio.run(_module_setup())
    return {}


async def _module_setup():
    await init_db()
