# Task 6: Orphan-requeue and Worker-Scaling Story

## Overview

This document describes the orphan-job recovery and worker-scaling mechanisms
for horizontal multi-replica safety in `rag-service`. The queue backend
abstraction (Task 5) provides the foundation; this task documents the
operational story.

## Orphan-Job Recovery

When a worker crashes or restarts while processing a job, the job may be
left in a partial state. The `recover_orphans()` method (defined in
`app.queue.QueueBackend`) handles recovery.

### `recover_orphaned_jobs()` Runbook

**When to run:**
- After unexpected worker crashes or process kills
- During planned maintenance/restarts
- On startup of new service replicas

**How it works:**
1. The `InlineBackend.recover_orphans()` returns `[]` — inline mode has no
   orphans since jobs run synchronously
2. For external backends (Redis/SQS/etc.), the implementation would:
   - Scan the queue for jobs in "processing" state beyond the timeout threshold
   - Transition them back to "pending" or "submitted" state
   - Log the recovery for observability

**Runbook steps:**
```bash
# 1. Check for orphaned jobs
export PATH="/home/ubuntu/rag-service/.venv/bin:$PATH"
python3 -c "
from app.queue import register_backend, get_backend, recover_orphans
register_backend('inline')
backend = get_backend()
orphans = backend.recover_orphans(older_than_seconds=300.0)
print(f'Recovered {len(orphans)} orphaned jobs')
for o in orphans:
    print(f'  - {o[\"job_id\"]}: {o[\"job_type\"]}')
"
```

**Multi-replica deployment:**
For Redis/RQ/Celery backends, the runbook would:
1. Use the backend's native tools (RQ `get_queue()`, Celery `revoke()`, etc.)
2. Re-queue jobs that were `started` but not `completed`/`failed`
3. Update monitoring dashboards to reflect recovery events

## Worker Scaling Story

### Single-Replica (InlineBackend)

- **One service instance** processes jobs inline
- No external queue infrastructure needed
- `submit()` runs the job function immediately and returns a job ID
- `get_next()` returns `None` (no polling needed)
- Ideal for development, testing, and small deployments

### Multi-Replica (External Backend)

When horizontal scaling is needed, substitute `InlineBackend` with an
external queue backend:

| Backend | Use Case | Notes |
|---|---|---|
| **Redis (RQ/Celery)** | General-purpose horizontal scaling | Mature ecosystem, retry/backoff built-in |
| **SQS** | AWS-native deployments | Managed service, automatic scaling |
| **Cloud Tasks** | GCP deployments | Integrated with GCP infrastructure |

**Scaling steps:**
1. Register the desired backend: `register_backend('redis')` or
   `register_backend('sqs')`
2. Ensure the external queue service is accessible from all replicas
3. All replicas share the same queue — `submit()` is idempotent across
   instances
4. Workers poll `get_next()` with appropriate timeout for their load
5. `mark_done()` completes the job lifecycle

**Benefits:**
- Multiple replicas can process jobs concurrently
- Job distribution is handled by the queue backend
- Orphan recovery is centralized in the queue service
- Horizontal scale without code changes to job logic

### Backend Substitution Pattern

```python
# Development/local: inline (default)
from app.queue import register_backend
register_backend('inline')

# Production horizontal: Redis
register_backend('redis')  # or custom RedisBackend subclass

# All API calls remain identical:
job_id = submit('ingest_doc', {'doc_id': 'abc123'})
next_job = get_next(timeout=60.0)
mark_done(job_id, {'status': 'completed'})
```

## Observability Integration

The queue backend integrates with `app.observability` for structured logging:

- `job_submitted`: job_id, job_type enqueued
- `job_completed`: job_id finished successfully
- `job_failed`: job_id encountered an error (tracked per-backend)
- `queue_backend_registered`: which backend is active

These logs can be aggregated for:
- Monitoring job throughput per backend
- Detecting orphan patterns (many recoveries = instability)
- Capacity planning for worker pools

## Migration Path

1. **Start** with `register_backend('inline')` — existing code works unchanged
2. **Test** with a development Redis/SQS instance
3. **Validate** `submit`/`get_next`/`mark_done` flow end-to-end
4. **Switch** production to the desired external backend
5. **Monitor** orphan recovery rates and adjust worker counts accordingly

## Related BACKLOG.md Items

- Item 29: `(Cloud Tasks / RQ / Celery) that calls the existing runner.
  Documented in requeue_orphaned_jobs().`
- Item 49: `[ ] Document requeue_orphaned_jobs() runbook for multi-replica.
  **done when** runbook`

## Design Decisions

1. **Backend-agnostic API:** `submit()`/`get_next()`/`mark_done()` etc. are
   identical across all backends — no code changes needed when switching
2. **Inline-first approach:** Default to InlineBackend; external backends
   are opt-in via `register_backend()` — lowers adoption friction
3. **Graceful degradation:** If an external backend is unavailable, the
   system can fall back to inline mode (or fail fast with a clear error)
4. **Observability-first:** Every operation logs structured data for
   monitoring and debugging