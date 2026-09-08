# Runbook Coverage: Missing Admin-Key in Docker Compose Workflow

## Failure Mode

The README "Run" section guides operators through:
```bash
1. docker compose up -d
2. uv venv && . .venv/bin/activate
3. uv pip install -e .
4. uvicorn app.main:app --port 8000
```

When operators follow these steps, **tenant creation fails with `403 "Admin key required"`** because the `ADMIN_API_KEY` environment variable is not loaded into the app container, even though `.env` contains `ADMIN_API_KEY=<YOUR_ADMIN_KEY>`.

**Root cause**: `docker-compose.yml` uses `env_file: - .env` and `ADMIN_API_KEY: ${ADMIN_API_KEY:-}`, but in the current runtime environment, the `.env` values are not being propagated to the app container. The `require_admin()` guard in `app/auth.py` returns 403 for all admin routes when `ADMIN_API_KEY` is empty.

## Step-by-Step Recovery

### Step 1: Verify .env has correct ADMIN_API_KEY
```bash
cd <your-repo-checkout>
cat .env | grep ADMIN_API_KEY
# Expected: ADMIN_API_KEY=<YOUR_ADMIN_KEY>
# If you see ADMIN_API_KEY=***, update it:
sed -i 's/^ADMIN_API_KEY=.*$/ADMIN_API_KEY=<YOUR_ADMIN_KEY>/' .env
```

### Step 2: Restart the app container with env_file loaded
```bash
# Stop the existing app container
docker compose stop app

# Start with env_file loaded (env_file is already in compose, just need fresh start)
docker compose up -d app

# Wait for service to be healthy
curl -s http://localhost:8000/health
# Expected: {"status":"ok","service":"rag-service","version":"1.0.0"}
```

### Step 3: Verify admin routes now work
```bash
# Tenant create (admin operation)
curl -s -X POST "http://localhost:8000/api/v1/tenants" \
  -H "Admin-Key: <YOUR_ADMIN_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"name":"test-tenant","plan":"shared"}'
# Expected: 201 {"tenant_id":"...","name":"test-tenant","api_key":"rk_...","plan":"shared","created_at":"...","chunk_count":0}
```

### Step 4: Create a secret key for the tenant
```bash
curl -s -X POST "http://localhost:8000/api/v1/keys" \
  -H "Admin-Key: <YOUR_ADMIN_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"prefix":"test-key"}'
# Expected: 201 {"doc_id":"...","key":"rk_...","created_at":"..."}
```

### Step 5: Make your first query (user operation)
```bash
# Use the secret key to query
curl -s -X POST "http://localhost:8000/api/v1/query" \
  -H "Authorization: Bearer rk_..." \
  -H "Content-Type: application/json" \
  -d '{"question":"What is the capital of France?", "mode": "dense"}'
# Expected: 200 {"answer":"Paris...","citations":[...],"mode":"dense","model":"...","usage":{...}}
```

## Summary

| Operation | Expected Auth | Expected Output |
|---|---|---|
| Tenant create | `Admin-Key: <YOUR_ADMIN_KEY>` | 201 Tenant created |
| Key create | `Admin-Key: <YOUR_ADMIN_KEY>` | 201 Key issued |
| Document query | `Authorization: Bearer <secret-key>` | 200 Query results |
| Publishable query | `Authorization: Bearer <publishable-key>` | Limited or 401 |

## Checklist: Docker Compose + Admin-Key Workflow

- [ ] `.env` has `ADMIN_API_KEY=<YOUR_ADMIN_KEY>` (not `***`)
- [ ] Service health: `curl -s http://localhost:8000/health` → 200
- [ ] Tenant create: `curl -s -X POST ... -H "Admin-Key: <YOUR_ADMIN_KEY>"` → 201
- [ ] Key create: `curl -s -X POST ... -H "Admin-Key: <YOUR_ADMIN_KEY>"` → 201
- [ ] First query: `curl -s -X POST ... -H "Authorization: Bearer ..."` → 200
- [ ] Query returns answer with citations

## Key Takeaway

The most critical onboarding concept is ensuring `ADMIN_API_KEY` is both set in `.env` **and** loaded into the running app container via `docker compose up -d`. Without this, all admin routes (tenant/create, key/create, key rotation, offboard) return 403 "Admin key required" with no indication of the missing configuration.

## Related Links

- `docs/onboarding_friction_runbook.md` — tenant-create to first-query path (auth mechanisms)
- `docs/operator_doc_drift_runbook.md` — drift identification
- `docs/operator_runbook_continuity_runbook.md` — continuity pass
- `README.md` line 63 — `ADMIN_API_KEY=<YOUR_ADMIN_KEY>` (corrected from `***`)
- `docker-compose.yml` — `env_file: - .env`, `ADMIN_API_KEY: ${ADMIN_API_KEY:-}`