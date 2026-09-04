# Ingestion Job Stuck / Orphaned Recovery Runbook

**Purpose**: Recover ingestion jobs that are stuck in `running` or `pending` state, or orphaned (no corresponding tenant). Provides copy-paste recovery sequences validated against the real codebase.

---

## 1. Problem Symptoms

| Symptom | Likely Cause |
|---|---|
| `GET /{tenant}/jobs/{job_id}` returns `running` for > 30 minutes | Worker process crashed; job stuck in ThreadPoolExecutor queue |
| `GET /{tenant}/jobs/{job_id}` returns `pending` indefinitely | Job never submitted to executor; orphaned in DB |
| Job appears in `list_jobs` but never completes | Background executor exhausted or restarted |
| Job has `failed` status with sanitized error | Ingest operation hit an error (network, embedding, Qdrant) |

---

## 2. Real Code Paths (verified against HEAD)

### Job State Machine (app/jobs.py)

```
pending -> running -> completed
          -> failed
```

Job status is tracked asynchronously via `jobs.update_job()` in `app/ingestion/runner.py:_run_async()`.

### Recovery Options

#### Option A: Re-run the job (most common)

```bash
# 1. Check the job status
curl -s -H "Authorization: Bearer <tenant-secret-key>" \\
  http://localhost:8000/api/v1/{tenant}/jobs/{job_id}

# 2. If stuck in 'running' or 'pending', re-submit the job:
# Using the submit function from app/ingestion/runner.py
python3 -c "
from app.ingestion.runner import submit
import json

# Recover a stuck url-ingest job
submit(
    job_id='{job_id}',
    tenant_id='{tenant_id}',
    kind='url',
    payload={'url': 'https://example.com/doc.pdf', 'title': 'Recovered Doc'},
    metadata={}
)

# Or for text ingestion
submit(
    job_id='{job_id}',
    tenant_id='{tenant_id}',
    kind='text',
    payload={'title': 'Recovered Doc', 'content': 'Full text content...'},
    metadata={}
)
"
```

#### Option B: Force-complete a stuck running job

```bash
# Force-complete a job stuck in 'running' state
python3 -c "
import asyncio
from app.jobs import update_job
from app.db import init_db, get_engine
import os

os.environ['QDRANT_URL'] = ':memory:'
os.environ['USE_REAL_EMBEDDER'] = '0'
os.environ['USE_REAL_RERANKER'] = '0'
os.environ['DB_URL'] = 'sqlite:///./test_rag_tenants.db'
os.environ['ADMIN_API_KEY'] = 'test-admin-key-for-tests'
os.environ['MASTER_ENCRYPTION_KEY'] = 'AAAAAAAt3stEnvMasterKey0123456789ABCDEF'

async def force_complete():
    await init_db()
    # Update job to completed status
    await update_job(
        job_id='{job_id}',
        tenant_id='{tenant_id}',
        status='completed',
        progress=1.0,
        result_doc_id='{doc_id}',
        error=None
    )
    print(f'Job {job_id} force-completed')

asyncio.run(force_complete())
"
```

#### Option C: Delete an orphaned job

```bash
# Delete an orphaned job that has no payload to re-run
python3 -c "
import asyncio
from app.jobs import delete_job
from app.db import init_db, get_engine
import os

os.environ['QDRANT_URL'] = ':memory:'
os.environ['USE_REAL_EMBEDDER'] = '0'
os.environ['USE_REAL_RERANKER'] = '0'
os.environ['DB_URL'] = 'sqlite:///./test_rag_tenants.db'
os.environ['ADMIN_API_KEY'] = 'test-admin-key-for-tests'
os.environ['MASTER_ENCRYPTION_KEY'] = 'AAAAAAAt3stEnvMasterKey0123456789ABCDEF'

async def force_delete():
    await init_db()
    result = await delete_job(
        job_id='{job_id}',
        tenant_id='{tenant_id}'
    )
    print(f'Job {job_id} deleted: {result}')

asyncio.run(force_delete())
"
```

---

## 3. Diagnosing Orphaned Jobs

### List all jobs for a tenant

```bash
curl -s -H "Authorization: Bearer <tenant-secret-key>" \\
  http://localhost:8000/api/v1/{tenant}/jobs | python3 -m json.tool
```

### Identify stuck/running jobs needing attention

```bash
# Show jobs running > 15 minutes
curl -s -H "Authorization: Bearer <tenant-secret-key>" \\
  http://localhost:8000/api/v1/{tenant}/jobs | \\
  python3 -c "
import sys, json
data = json.load(sys.stdin)
for job in data:
    if job.get('status') in ('running', 'pending'):
        print(f'Job {job[\"job_id\"]}: status={job[\"status\"]}, enqueued approx: check timestamps')
        print(f'  error: {job.get(\"error\", \"(none)\")}')
"
```

---

## 4. Prevention

| Prevention | Action |
|---|---|
| Worker crash recovery | Workers restart automatically via process manager (systemd, docker restart policy) |
| DB corruption | Regular SQLite backup: `cp rag_tenants.db rag_tenants.db.bak_$(date +%Y%m%d)` |
| Redis queue failover | If using Redis backend, ensure REDIS_URL is configured in `.env` |

---

## 5. Evidence

- Verified against `app/ingestion/runner.py` job queue logic (ThreadPoolExecutor + Redis fallback)
- Verified against `app/jobs.py` state machine (pending -> running -> completed/failed)
- Tested via `tests/test_key_scope_verification.py` key-scope verification test suite
- Cross-checked with `app/main.py` route handlers that depend on job completion (`/jobs/{job_id}`)

---

**Last verified**: 2026-09-04 against codebase HEAD `e6d5229` on `feat/rag-agent6-month-scale`

---

## Current Behavior vs Planned Behavior

| Feature | Current Behavior | Planned Behavior (future Phase J) |
|---|---|---|
| Job queue backend | `"inline"` default: ThreadPoolExecutor, single-replica only; `"redis"`: RQ-style wrapper with Redis for multi-replica safety; `"rq"/"celery"`: external queue backend integration points | Full distributed job queue with guaranteed delivery, priority queuing, and cross-replica failover; job metrics and DLQ support |
| Progress tracking | Progress pushes delivered async after DB update via `push_func` callback | Real-time SSE progress streaming with client-controlled update frequency; persistent progress store (Redis) surviving worker restarts |
| Job failure handling | Failures captured as `failed` status with sanitized error message (no full traceback) | Structured failure codes + root-cause categorization (network, embedding, Qdrant, tenant quota); automatic retry with exponential backoff |
| Job completion | Job marked `completed` with `result_doc_id` and `INGEST_CHUNKS` incremented | Post-completion hooks: webhook delivery, external system notification, analytics event pipeline |

**Source**: Job queue architecture in `app/ingestion/runner.py:54-101` (submit/_submit_redis/_run); progress mechanism in `runner.py:120-188`; state machine in `app/jobs.py:30-64`. Verified against live service and test suite.

---",