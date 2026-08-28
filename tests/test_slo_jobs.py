"""v9-5: SLOs/alerting + durable job orchestration verification."""
import os

import pytest

from app.db import create_job, requeue_orphaned_jobs, get_job, update_job
from app.config import get_settings

V = "/api/v1"


def test_slo_endpoint_reports_status(client):
    """v9-5: /health/slo returns current availability + p95 vs configured targets."""
    r = client.get("/health/slo")
    assert r.status_code == 200
    body = r.json()
    assert "availability" in body and "latency_p95_s" in body
    assert "availability_target" in body and "latency_target_s" in body
    assert body["status"] in ("ok", "breach")


def test_metrics_expose_slo_series(client):
    """v9-5: SLO metrics are exported in /metrics (admin-gated)."""
    r = client.get("/metrics", headers={"Admin-Key": os.environ.get("ADMIN_API_KEY")})
    assert r.status_code == 200
    assert b"rag_slo_requests_total" in r.content
    assert b"rag_slo_latency_seconds" in r.content


def test_job_recovery_resets_orphaned_running_jobs():
    """v9-5: durable job orchestration — jobs stranded in `running` by a crashed worker
    are recovered (reset to `pending`) on startup/restart, so they get retried."""
    import asyncio

    async def _run():
        # create a job, drive it to running, then simulate a crash + recovery.
        from app.db import init_db
        await init_db()
        tid = "t_recovery"
        jid = await create_job(tid, "document", "doc1")
        await update_job(jid, status="running", progress=0.5)
        before = await get_job(jid, tid)
        assert before is not None
        assert before["status"] == "running"
        recovered = await requeue_orphaned_jobs()
        after = await get_job(jid, tid)
        assert after is not None
        return recovered, after["status"]

    recovered, status = asyncio.run(_run())
    assert recovered >= 1
    assert status == "pending"
