# Runbook: requeue_orphaned_jobs

## Purpose
Reset orphaned `running` jobs back to `pending` status on service restart/recovery. A job left in `running` when a worker died (OOM, deploy, crash) would otherwise be stuck forever.

## When to Run
- On service startup after an unclean shutdown
- After a deployment where workers may have crashed mid-ingestion
- After an OOM kill or other worker failure
- Periodically as part of routine maintenance (recommended weekly)

## Procedure

### Single-Replica VPS Case (Inline Backend)
1. Ensure the service is running (or start it)
2. Import and call the recovery function:

```python
from app.db import requeue_orphaned_jobs
recovered = await requeue_orphaned_jobs()
print(f"Recovered {recovered} orphaned jobs")
```

3. Verify recovered jobs are now in `pending` status:

```python
from app.jobs import list_jobs
pending_jobs = await list_jobs(tenant_id, limit=100)
running_jobs = [j for j in pending_jobs if j["status"] == "pending"]
```

### Multi-Replica Case (Redis/RQ Backend)
When using Redis-backed job queue (Phase J, Task 48):
- Jobs are stored in the durable Redis queue, so they are naturally visible across all replicas
- No explicit requeuing needed — the RQ queue retains jobs across worker restarts
- On recovery, just ensure the RQ worker is restarted and will pick up pending jobs

## Expected Output
- Returns the number of recovered jobs (0 if no orphans exist)
- Each recovered job transitions from `running` → `pending`
- Workers can then pick up and re-process the recovered jobs

## Safety Notes
- This operation is idempotent — running it multiple times is safe
- Only jobs in `running` status are reset; `pending`, `completed`, and `failed` jobs are unaffected
- The jobs table (SQLite/Postgres) is the durable source of truth
- For horizontal scale with N workers, front the jobs table with an at-least-once queue (Cloud Tasks / RQ / Celery) that calls the existing job runner

## Integration with Phase J Tasks
- **Task 47**: SSRF-guarded tenant callback URLs — already implemented in `app/ingestion/ssrf.py`
- **Task 48**: JOB_QUEUE backend setting — see `app/config.py` and `app/ingestion/runner.py`
- **Task 49**: This runbook — being documented now
- **Task 50**: Tag `v16.56-ingestion-scale` — see below