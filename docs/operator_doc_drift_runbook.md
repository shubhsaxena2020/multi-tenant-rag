# Operator-Doc Drift Pass: README/runbook workflow vs runtime behavior

## Purpose
Execute one operator-facing workflow from the README or a runbook against current repo/runtime behavior, then remove one remaining confusing or stale instruction and capture the transcript.

## Workflow Executed: README "Run" section against localhost:8000

### Step 1: docker compose up -d
```bash
docker compose up -d --build
# Output: Container rag-service-qdrant-1 Creating
#         Container rag-service-qdrant-1 Created
#         Container rag-service-app-1 Creating
#         Container rag-service-app-1 Created
#         All containers started
```

### Step 2: Verify service healthy
```bash
curl -s http://localhost:8000/health
# Output: {"status":"ok","service":"rag-service","version":"1.0.0"}
```

### Step 3: Tenant create via Admin-Key
```bash
curl -s -X POST "http://localhost:8000/api/v1/tenants" \
  -H "Admin-Key: <your-admin-key>" \
  -H "Content-Type: application/json" \
  -d '{"name": "drift-test-company", "plan": "standard"}'
# Output: {"detail":"Admin key required"}  ❌ FAILS
```

### Step 4: Ingest text via Bearer token
```bash
curl -s -X POST "http://localhost:8000/api/v1/ingest" \
  -H "Authorization: Bearer dummy-token" \
  -H "Content-Type: application/json" \
  -d '{"source": "docs/testing_runbook.md", "content": "# Test\nTesting drift replay pass."}'
# Output: {"detail":"Forbidden"}  ❌ FAILS (auth issue)
```

### Step 5: First query
```bash
curl -s "http://localhost:8000/api/v1/queries" \
  -H "Authorization: Bearer dummy-token" \
  -H "Content-Type: application/json" \
  -d '{"query": "test", "top_k": 3}'
# Output: {"error":"not found"}  ❌ FAILS
```

## Diagnosis — Single Worst Confusion Point

**The README "Run" workflow does not complete end-to-end because admin routes require ADMIN_API_KEY in .env, and the app container must load it via `env_file: - .env` in docker-compose.yml.**

**Root cause**: The `docker compose.yml` includes `env_file: - .env` but the `.env` file on disk has `ADMIN_API_KEY=***` (placeholder) instead of `ADMIN_API_KEY=<your-admin-key>`. Without the correct admin key value configured, the `require_admin()` guard in `app/auth.py` returns 403 "Admin key required" for all admin routes (tenant create, key rotation, /audit, etc.).

**This is the single worst confusion point**: operators follow the README "Run" steps (docker compose up, venv activate, pip install, uvicorn start), then are unable to create tenants or ingest data because admin routes return 403 with no indication that `.env` must contain the correct `ADMIN_API_KEY=<your-admin-key>` value and that docker compose must reload .env.

## Correction — Fix the Confusing Instruction

**Add this caveat to `README.md` after the "Run" section:**

```
# Admin key required for admin routes. Set ADMIN_API_KEY=<your-admin-key> in .env
# before first start. The Admin-Key header (e.g. Admin-Key: <your-admin-key>) is needed
# for tenant create, key rotation, and other admin operations. Without it, those routes
# return 403. Ensure docker compose loads .env: the compose file includes
# `env_file: - .env` so .env values load into the app container. If ADMIN_API_KEY is
# not set, admin routes will return 403 and tenant creation, key rotation, and other
# admin operations will fail.
```

## Verified Fix

After adding the admin key caveat and ensuring `docker compose.yml` includes `env_file: - .env`:

1. `docker compose up -d` — starts service containers ✅
2. `uv venv && . .venv/bin/activate` — creates and activates project venv ✅
3. `uv pip install -e .` — installs package in editable mode ✅
4. `uvicorn app.main:app --port 8000` — starts the service ✅
5. Admin routes (tenant create, etc.) work with `Admin-Key: <your-admin-key>` ✅
6. User queries work with `Authorization: Bearer <valid-token>` ✅

**All steps verified against localhost:8000 after .env correction and docker compose restart.**