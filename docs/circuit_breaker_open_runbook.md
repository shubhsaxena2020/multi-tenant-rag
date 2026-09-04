# Circuit Breaker OPEN — Runbook

## Symptom

- Queries to the RAG service return `degraded` responses with public detail:
  `"{dependency} temporarily unavailable (degraded mode)"`
- Health endpoint `/health` shows the affected dependency as `open`
- Metrics at `/metrics` show circuit breaker state changes (Prometheus `circuit_breaker_state` metric)
- All requests to the affected dependency fast-fail with `CircuitOpen` error

## Diagnosis

1. **Check circuit breaker status** via metrics or API:

   ```bash
   # Query Prometheus for circuit breaker state
   curl -s http://localhost:8000/metrics | grep circuit_breaker_state
   ```

   Or check the `/health` endpoint:

   ```bash
   curl -s http://localhost:8000/health
   ```

2. **Verify the dependency is actually unhealthy**:

   - Qdrant: check Qdrant container health, disk space, network connectivity
     ```bash
     docker compose logs qdrant --tail 20
     docker stats qdrant --no-stream
     ```
   - Embedder: check model load, memory availability
   - Reranker: check model availability, TEI URL connectivity

3. **Confirm circuit breaker state** via code or metrics:

   ```python
   from app.resilience import circuit_status
   # circuit_status("qdrant") returns "open", "half_open", or "closed"
   ```

## Remediation

### Step 1: Identify and fix the root cause

The circuit breaker trips OPEN after `cb_failure_threshold` (default: 5) consecutive failures,
followed by a cooldown period (`cb_cooldown_s`, default: 30s). Common causes:

- **Qdrant down or unreachable**: network partition, container crash, disk full
- **Embedder OOM**: model loads fails, memory exhaustion
- **Reranker timeout**: TEI node unreachable, model inference hangs

Fix the underlying issue first, then reset the circuit breaker.

### Step 2: Reset the circuit breaker

Once the root cause is resolved, reset the breaker state:

```python
from app.resilience import reset_breakers
reset_breakers()
```

Or via CLI (if exposed):

```bash
# If there's an admin endpoint to reset breakers, use it
# Otherwise, restart the affected dependency or the app container
docker compose restart app
```

### Step 3: Verify recovery

1. Check that the circuit breaker has closed:

   ```bash
   curl -s http://localhost:8000/metrics | grep circuit_breaker_state
   ```

2. Run a test query to confirm the dependency is healthy:

   ```bash
   curl -s http://localhost:8000/health
   ```

3. Run the full test suite to ensure no regressions:

   ```bash
   .venv/bin/python -m pytest -q
   ```

## Recovery Transcript (example)

```bash
# 1. Check current state
$ curl -s http://localhost:8000/health
{"detail":"circuit breaker OPEN for qdrant"}

# 2. Verify Qdrant is down
$ docker compose logs qdrant --tail 10
qdrant            | shutdown initiated...
# or
$ docker stats qdrant --no-stream
# shows Qdrant not running

# 3. Start Qdrant
$ docker compose up -d qdrant
$ sleep 10

# 4. Reset the circuit breaker
$ .venv/bin/python -c "from app.resilience import reset_breakers; reset_breakers()"
# or just restart the app
$ docker compose restart app

# 5. Verify recovery
$ curl -s http://localhost:8000/health
{"status":"ok","circuit_breakers":{"qdrant":"closed","embedder":"closed","reranker":"closed"}}

# 6. Run a test query
$ curl -s http://localhost:8000/api/v1/health/deps
```

## Related Runbooks

- [`docs/qdrant_unhealthy_snapshot_restore_runbook.md`](../qdrant_unhealthy_snapshot_restore_runbook.md) —
  for Qdrant hardware/storage failures requiring snapshot restore
- [`docs/operate_rag_service.md`](../operate_rag_service.md) — one-page operational overview
- `app/resilience.py` — circuit breaker configuration and state management

## Prevention

- Monitor circuit breaker state via Prometheus: `circuit_breaker_state{dependency="qdrant"}`
- Set up alerts when a breaker trips OPEN
- Ensure Qdrant/embedder/reranker have health checks and auto-restart policies
- Review `app/config.py` `cb_failure_threshold` and `cb_cooldown_s` settings for your deployment scale