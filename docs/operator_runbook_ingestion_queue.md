# Operator Runbook: Inline vs. Queue-Backed Ingestion Behavior

## Overview

This document explains how to distinguish **inline-only** ingestion behavior from **durable queue-backed** behavior in the currently shipped code. The default configuration uses inline execution, but the system supports Redis-backed and RQ/Celery-backed queue modes for multi-replica safety.

---

## 1. Current Default: Inline Execution

### Configuration
```bash
# app/config.py:97
job_queue_backend: str = "inline"

# job_queue_connection is empty by default
job_queue_connection: str = ""
```

### What "inline" means
- **Execution**: Jobs run in a `ThreadPoolExecutor` with `max_workers=4` (`app/ingestion/runner.py:43-48`)
- **Scope**: Single-replica only; each app instance has its own executor
- **Job submission**: `submit()` routes to the inline executor when no Redis/rq/celery backend is configured (`runner.py:158-160`)
- **Visibility**: Jobs are only visible within the process that submitted them
- **Durability**: If the worker crashes or the process restarts, running jobs may be lost unless recovered by the startup `requeue_orphaned_jobs` mechanism

### Commands to verify inline mode
```bash
# Check the effective configuration at runtime
python -c "from app.config import get_settings; s = get_settings(); print(f'backend={s.job_queue_backend}, conn={s.job_queue_connection}')"

# Confirm ThreadPoolExecutor is in use
# The _ensure_executor() creates a ThreadPoolExecutor with max_workers=4
```

---

## 2. Queue-Backed: Redis Mode

### Configuration
```bash
# .env or environment
job_queue_backend="redis"
job_queue_connection="redis://localhost:6379/0"
```

### What "redis" means
- **Execution**: Jobs are enqueued to an RQ-style Redis queue (`runner.py:61-91`)
- **Multi-replica safety**: The Redis queue is shared across all app replicas; any worker can dequeue and process jobs
- **Durability**: Jobs survive worker crashes and process restarts; Redis persists until explicitly removed
- **Idempotency**: Same job-id + payload can be re-enqueued safely

### Commands to verify Redis mode
```bash
# Confirm redis backend is detected
python -c "
from app.ingestion.runner import _is_redis_backend
from app.config import get_settings
s = get_settings()
print(f'_is_redis_backend()={_is_redis_backend()}')
print(f'job_queue_backend={s.job_queue_backend}')
print(f'job_queue_connection={s.job_queue_connection}')
"

# Verify jobs are enqueued to Redis
# Submit a job and check Redis queue length
redis-cli LLEN ingest  # should show job count

# Or via API
curl -X POST http://localhost:8000/{tenant}/ingest/jobs \
  -H "Authorization: Bearer {api_key}" \
  -H "Content-Type: application/json" \
  -d '{"kind": "text", "title": "test"}'
# Then check job status via GET /{tenant}/jobs/{job_id}
```

---

## 3. Queue-Backed: RQ Mode

### Configuration
```bash
# .env or environment
job_queue_backend="rq"
job_queue_connection="redis://localhost:6379/0"
```

### What "rq" means
- **Execution**: Same Redis queue mechanism as `redis` backend, labeled differently in metrics (`runner.py:156-157`)
- **Metrics**: `INGEST_JOBS.labels(backend="rq")` for observability
- **Same durability** as `redis` mode since it uses the same underlying Redis queue

### Commands to verify RQ mode
```bash
# Same verification as Redis mode, but check metrics label
python -c "
from app.ingestion.runner import _is_rq_or_celery_backend
from app.config import get_settings
s = get_settings()
print(f'_is_rq_or_celery_backend()={_is_rq_or_celery_backend()}')
print(f'job_queue_backend={s.job_queue_backend}')
"

# Check that jobs are labeled as 'rq' backend
# In Prometheus: INGEST_JOBS{backend=\"rq\"} should increment
```

---

## 4. Queue-Backed: Celery Mode

### Configuration
```bash
# .env or environment
job_queue_backend="celery"
job_queue_connection="redis://localhost:6379/0"  # or rabbitmq URL
# Also need CELERY_BROKER_URL set
```

### What "celery" means
- **Execution**: Jobs sent to Celery task queue via `celery.current_app.send_task()` (`runner.py:128-133`)
- **Multi-replica safety**: Celery workers across any number of replicas can process jobs
- **Durability**: Full Celery persistence/acknowledgment guarantees
- **Integration**: Requires Celery app instance `app.ingestion.runner.celery` configured

### Commands to verify Celery mode
```bash
# Check celery integration
python -c "
from app.ingestion.runner import _is_rq_or_celery_backend
from app.config import get_settings
s = get_settings()
print(f'_is_rq_or_celery_backend()={_is_rq_or_celery_backend()}')
"

# Submit job and verify Celery task
# The job is sent as: app.ingestion.runner._run_celery
curl -X POST http://localhost:8000/{tenant}/ingest/jobs \
  -H "Authorization: Bearer {api_key}" \
  -H "Content-Type: application/json" \
  -d '{"kind": "text", "title": "test"}'
# Check: INGEST_JOBS.labels(backend=\"celery\") increments
```

---

## 5. How to Distinguish Inline vs. Queue-Backed — Operator Checklist

### A. Check Configuration
```bash
# 1. Read the effective settings
python -c "from app.config import get_settings; s = get_settings()
print(f'JOB_QUEUE_BACKEND={s.job_queue_backend}')
print(f'JOB_QUEUE_CONNECTION={s.job_queue_connection}')
print(f'proxy_enabled={s.proxy_enabled}')
print(f'proxy_url={s.proxy_url}')
"

# 2. Or via environment introspection
echo "JOB_QUEUE_BACKEND=$job_queue_backend"
echo "JOB_QUEUE_CONNECTION=$job_queue_connection"
```

### B. Submit a Test Job and Observe Behavior
```bash
# Submit a job via the API
JOB_ID=$(curl -s -X POST http://localhost:8000/{tenant}/ingest/jobs \
  -H "Authorization: Bearer {api_key}" \
  -H "Content-Type: application/json" \
  -d '{"kind": "text", "title": "test-job"}' | jq -r '.job_id')

# Poll job status
for i in $(seq 1 30); do
  STATUS=$(curl -s http://localhost:8000/{tenant}/jobs/${JOB_ID} \
    -H "Authorization: Bearer {api_key}" | jq -r '.status')
  echo "Attempt $i: status=$STATUS"
  if [ "$STATUS" != "pending" ]; then
    break
  fi
  sleep 1
done

# 3. Check job metadata for queue evidence
curl -s http://localhost:8000/{tenant}/jobs/${JOB_ID} \
  -H "Authorization: Bearer {api_key}" | jq .
```

### C. Inline Mode Evidence
- Job status transitions: `pending -> running -> completed` within the same process
- No persistent job ID across process restarts
- `INGEST_JOBS.labels(status=...) increments locally only
- ThreadPoolExecutor workers complete, then jobs are lost if process exits without saving

### D. Queue-Backed Mode Evidence
- Job ID persists across process restarts (recovered by `requeue_orphaned_jobs` on startup)
- `INGEST_JOBS.labels(backend="redis"|"rq"|"celery")` increments in Prometheus
- Job status can be checked after worker/process restart
- Redis queue retains jobs: `redis-cli LLEN ingest` shows queued items
- Celery/RQ worker logs show job dequeue and execution

---


## 5. Alerting Thresholds (monitor-006)

### Sitemap crawl latency alert
- Alert when `rag_sitemap_crawl_duration_seconds` p50 > 5s or p95 > 15s over 5-minute window
- Fires on `rag_sitemap_crawl_duration_seconds` sustained > 0 for 5m

### Duplicate detection alert
- Alert when `rag_duplicate_detections_total` > 0 for 5-minute window (indicates crawl/config issue)
- Fires on `rag_duplicate_detections_total` sustained > 0 for 5m

### Robots rate-limit alert
- Alert when `rag_robots_rate_limit_exceeded_total` > 0 for 5-minute window (indicates robots.txt config issue)
- Fires on `rag_robots_rate_limit_exceeded_total` sustained > 0 for 5m

### Queue depth alert
- Alert when `rag_ingest_queue_depth` > 100 for `backend="redis"` (queue buildup)
- Alert when `rag_ingest_queue_depth` > 1000 for `backend="rq"` (urgent buildup)
- Fires on sustained > 0 for 5m

### Sustained incident alert
- Alert when `rag_release_incidents_total` > 0 for 1-hour window (confirmed incident via /metrics)
- Fires on `rag_release_incidents_total` sustained > 0 for 1h

_All alerts paired with runbook Section 4 diagnosis commands for rapid triage._
## 6. Orphan Job Recovery (Startup Behavior)

### What happens on app startup
```python
# app/main.py:144-157 — v9-5 lifespan hook
@asynccontextmanager
async def _lifespan(app: FastAPI):
    try:
        from .db import init_db, requeue_orphaned_jobs
        await init_db()
        recovered = await requeue_orphaned_jobs()
        if recovered:
            log.info("jobs_recovered_on_startup", extra={"recovered": recovered})
    except Exception as e:
        log.warning("startup_recovery_failed", extra={"error_type": type(e).__name__})
    yield
```

- **Inline mode**: Orphaned `running` jobs are lost (no persistent store for in-flight thread pool tasks)
- **Queue-backed mode**: Orphaned `running` jobs are reset to `pending` and retried automatically
- **Verification**: Restart the app and check that previously-submitted queue-backed jobs reappear as `pending`

### Command to test recovery
```bash
# 1. Submit a queue-backed job (redis/rq/celery mode)
# 2. Kill the app process
# 3. Restart the app
# 4. Check: GET /{tenant}/jobs should show the job as `pending` (recovered)
# 5. Without queue mode: job is lost, not recovered
```

---

## 7. Summary: Distinguishing Inline vs. Queue-Backed

| Evidentiary Category | Inline Mode | Queue-Backed (Redis/RQ/Celery) |
|---|---|---|
| **Config** | `job_queue_backend=inline`, empty `job_queue_connection` | `job_queue_backend=redis|rq|celery`, non-empty `job_queue_connection` |
| **Job persistence** | Lost on process exit/restart | Survives restarts via Redis/Celery |
| **Multi-replica** | Each instance independent | Shared queue across all replicas |
| **Orphan recovery** | No recovery; jobs lost on crash | Automatic recovery via `requeue_orphaned_jobs` |
| **Prometheus metrics** | `INGEST_JOBS` without `backend` label | `INGEST_JOBS{backend=\"redis\"|\"rq\"|\"celery\"}` |
| **Redis queue inspection** | N/A (no Redis usage) | `redis-cli LLEN ingest` shows job count |
| **Job ID persistence** | Tied to process lifecycle | Persistent across restarts |

---

## 8. Migration Path: Inline → Queue-Backed

### Steps to switch from inline to Redis-backed

1. **Configure Redis**
   ```bash
   # .env
   job_queue_backend="redis"
   job_queue_connection="redis://localhost:6379/0"
   ```

2. **Restart the application**
   - New jobs will be enqueued to Redis
   - Existing inline jobs are lost (expected)

3. **Verify the switch**
   ```bash
   # Confirm backend detection
   python -c "from app.ingestion.runner import _is_redis_backend; print(_is_redis_backend())"
   
   # Confirm jobs go to Redis
   redis-cli LLEN ingest  # should > 0 after submitting a job
   ```

3. **Scale to multiple replicas**
   - All replicas share the same Redis queue
   - Any worker can process any job
   - Job status is consistent across all instances

### Steps to switch from Redis to inline (downgrade)

1. **Update config**
   ```bash
   # .env
   job_queue_backend="inline"
   job_queue_connection=""
   ```

2. **Flush Redis queue** (optional, for clean state)
   ```bash
   redis-cli DEL ingest
   ```

3. **Restart the application**
   - New jobs run inline only
   - Queue-backed jobs in Redis become orphaned and are NOT automatically recovered by inline mode

## 4. Crawl Backlog Diagnosis

### When to Use
- Sitemap crawl completes but `urls_ingested` count doesn't match expected
- Documents not appearing in DocumentCatalogPage after sitemap ingestion
- Orphaned running jobs from failed/incomplete crawls
- Queue depth anomalies after sitemap ingestion cycles

### Diagnosis Commands

#### Check Sitemap Crawl Results
```bash
# Verify the sitemap crawl returned expected url count
python -c "
from app.ingestion.sitemap import crawl_sitemap
result = crawl_sitemap('http://example.com/sitemap.xml')
print(f'urls_found: {result.get("urls_found", "N/A")}')
print(f'urls_ingested: {result.get("urls_ingested", "N/A")}')
print(f'items_in_catalog: {result.get("items_in_catalog", "N/A")}')
"

# Via API
curl -X POST http://localhost:3002/sitemap/crawl \
  -H "Content-Type: application/json" \
  -d '{"sitemap_url": "http://example.com/sitemap.xml"}'
```

#### Check DocumentCatalogPage Item Count
```bash
# Verify documents are visible in catalog after ingestion
python -c "
from app.ingestion.crawler import DocumentCatalogPage
# List documents and check items count
docs = await DocumentCatalogPage.list_documents(tenant_id)
print(f'Total documents in catalog: {len(docs.items)}')
for doc in docs.items[:5]:
    print(f'  - {doc.doc_key}: status={doc.status}')
"

# Via API endpoint
curl -X GET http://localhost:8000/{tenant}/documents
```

#### Check for Orphaned Running Jobs
```bash
# Recover any running jobs stuck from incomplete crawls
python -c "
from app.jobs import requeue_orphaned_jobs
recovered = await requeue_orphaned_jobs()
print(f'Recovered {recovered} orphaned running jobs)
"

# Verify job status after requeue
python -c "
from app.jobs import list_jobs
jobs = await list_jobs(tenant_id, limit=100)
running = [j for j in jobs if j['status'] == 'running']
print(f'Running jobs: {len(running)}
if running:
    for j in running:
        print(f'  - Job {j["job_id"]}: {j["kind"]} from {j["source"]})
"

# Via API
curl -X POST http://localhost:8000/{tenant}/jobs/requeue_orphaned
```

#### Verify Queue Depth After Ingestion
```bash
# Check inline executor queue depth
python -c "
from app.ingestion.runner import get_queue_depth
depth = get_queue_depth()
print(f'Queue depth: {depth})
"

# Check Redis queue length if queue-backed
redis-cli LLEN ingest  # jobs in Redis queue
```

### Expected Outcomes
- **Sitemap crawl**: `urls_ingested` should match expected document count
- **DocumentCatalogPage**: `len(docs.items)` should equal ingested count
- **Orphaned jobs**: `recovered` count should be 0 after clean runs, or N after partial failures
- **Queue depth**: Should be 0 or match expected backlog after processing




## 5. Alerting Thresholds

### When to Use
- Prometheus alerts firing unexpectedly or not firing when expected
- Monitor dashboards showing anomalies in crawl/ingestion behavior
- Alerts for queue depth, job failure rates, or sitemap crawl errors
- Threshold violations during scale-up or scale-down events

### Diagnosis Commands

#### Check Prometheus Alert Rules
```bash
# List all alert rules
curl -X GET http://localhost:9090/api/v1/rules

# Check specific alert rules related to ingestion
curl -X GET http://localhost:9090/api/v1/rules | python -c "
import json, sys
data = json.load(sys.stdin)
for group in data['data']['result']:
    for rule in group['values'][1:]:
        print(f'{group["labels"].get("alert", "N/A")}: {rule["metric"]}')
"

#### Check Alert Evaluation
```bash
# Check if an alert is currently firing
curl -X GET http://localhost:9090/api/v1/alerts?match[]=ingest_error

# Check alert history
curl -X GET http://localhost:9090/api/v1/alerts
```

#### Verify Ingestion Health Metrics
```bash
# Check sitemap crawl latency
python -c "
from app.ingestion.metrics import INGEST_LATENCY_SECONDS
# Check latest values
print(f'Latest latency: {INGEST_LATENCY_SECONDS._value.get() if INGEST_LATENCY_SECONDS._value._value else "N/A"}')
"

# Check duplicate detection rate
python -c "
from app.ingestion.metrics import DUPLICATE_COUNTER
print(f'Duplicates: {DUPLICATE_COUNTER._value.get() if DUPLICATE_COUNTER._value._value else "N/A"}')
"

# Check queue depth gauge
python -c "
from app.ingestion.metrics import QUEUE_DEPTH_GAUGE
print(f'Queue depth: {QUEUE_DEPTH_GAUGE._value.get() if QUEUE_DEPTH_GAUGE._value._value else "N/A"}')
"
```

#### Check Job Failure Rates
```bash
# Via API - check job status distribution
curl -X GET http://localhost:8000/{tenant}/jobs?status=failed | python -c "
import json, sys
data = json.load(sys.stdin)
failed = [j for j in data if j['status'] == 'failed']
print(f'Failed jobs: {len(failed)}')
print(f'Failure rate: {len(failed)/max(len(data),1)*100:.1f}%)
"

# Check running jobs count
curl -X GET http://localhost:8000/{tenant}/jobs?status=running | python -c "
import json, sys
data = json.load(sys.stdin)
running = [j for j in data if j['status'] == 'running']
print(f'Running jobs: {len(running)}')
"
```

### Expected Threshold Values (Baseline)

| Metric | Normal Range | Alert Threshold | Description |
|--------|-------------|-----------------|-------------|
| `ingest_latency_seconds` | < 30s per sitemap | > 60s | Unusually slow crawl |
| `duplicate_counter_total` | 0-5 per run | > 10 | Excessive duplicates detected |
| `queue_depth_gauge` | 0 or small | > 50 | Queue backing up |
| `running_jobs_count` | 0-4 (inline) | > 10 | Stuck or blocked jobs |
| `sitemap_urls_ingested` | Matches expected | < 50% expected | Crawl failure |
```

### Prometheus Rule Examples

```yaml
# alert-rules.yaml (example)
groups:
- name: ingestion-alerts
  rules:
  - alert: IngestLatencyHigh
    expr: histogram_quantile(0.95, rate(ingest_latency_seconds_bucket[5m])) > 60
    for: 1m
    labels:
      severity: warning
    annotations:
      summary: "Sitemap crawl latency is high (95th pct: >60s)"
      description: "Sitemap crawl has been running for over 60s at the 95th percentile"

  - alert: DuplicateDetectionHigh
    expr: rate(duplicate_counter_total[5m]) > 10
    for: 1m
    labels:
      severity: warning
    annotations:
      summary: "High duplicate detection rate"
      description: "More than 10 duplicates detected per minute"

  - alert: QueueDepthTooHigh
    expr: queue_depth_gauge > 50
    for: 1m
    labels:
      severity: critical
    annotations:
      summary: "Ingestion queue depth is too high"
      description: "Queue has more than 50 pending jobs, likely a backlog"
```

### Commands to Verify Alert Rules
```bash
# Load alert rules into Prometheus
curl -X POST http://localhost:9090/api/v1/rules -d @alert-rules.yaml

# Reload Prometheus configuration
curl -X POST http://localhost:9090/-/reload

# Check rule evaluation status
curl -X GET http://localhost:9090/api/v1/rules
```
