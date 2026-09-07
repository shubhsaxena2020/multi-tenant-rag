# Runbook: RAGAppDown / Service-Down Case

## Overview
This runbook documents the symptoms, checks, restart procedure, and verification steps for when the rag-service becomes unavailable or malfunctioning. It covers the common failure mode where admin routes return 403 "Admin key required" due to a misconfigured `.env` file.

## Symptoms
- `POST/GET/DELETE /api/v1/tenants` returns `403 {"detail":"Admin key required"}`
- `POST/GET/DELETE /api/v1/{tenant}/keys/*` returns `403 {"detail":"Admin key required"}`
- `POST/GET/DELETE /api/v1/{tenant}/documents*` returns `403 {"detail":"Admin key required"}`
- Health checks (`/health`, `/health/slo`) may still return 200
- Prometheus metrics endpoint (`/metrics`) still accessible
- Query routes (`POST /api/v1/{tenant}/query`) may still work with user API keys

## Root Cause
The `.env` file in the rag-service directory has `ADMIN_API_KEY=***` (placeholder) instead of the real value `admin_master_key`. The `require_admin()` guard in `app/auth.py` checks `provided != settings.admin_api_key`, and when they don't match, it returns 403.

## Checks

### 1. Verify the `.env` file value
```bash
cat .env | grep ADMIN_API_KEY
```
**Expected output**: `ADMIN_API_KEY=admin_master_key`

**Stale/broken output**: `ADMIN_API_KEY=***` or the line is missing/empty

### 2. Verify the running container has the correct value
```bash
docker compose exec app env | grep ADMIN_API_KEY
```
**Expected output**: `ADMIN_API_KEY=admin_master_key`

### 3. Verify docker-compose.yml references the .env file
```bash
grep "env_file" docker-compose.yml
```
**Expected output**: `env_file: - .env`

### 4. Test admin route without Admin-Key header (should fail)
```bash
curl -s -X POST http://localhost:8000/api/v1/tenants -H "Authorization: Bearer rk_test" -H "Content-Type: application/json" -d '{"name":"test"}'
```
**Expected output**: `{"detail":"Admin key required"}`

### 5. Test admin route WITH correct Admin-Key header (should succeed)
```bash
curl -s -X POST http://localhost:8000/api/v1/tenants -H "Authorization: Bearer rk_test" -H "Admin-Key: admin_master_key" -H "Content-Type: application/json" -d '{"name":"test"}'
```
**Expected output**: `{"tenant_id":"...","name":"test","api_key":"...","plan":"shared","created_at":"...","chunk_count":0}`

## Restart Procedure

### Step 1: Fix the `.env` file
Replace the placeholder value with the real admin key:
```bash
# If using sed (careful with special characters)
sed -i 's/ADMIN_API_KEY=***$/ADMIN_API_KEY=admin_master_key/' .env

# Or simply edit the file directly
# Ensure the .env file contains:
ADMIN_API_KEY=admin_master_key
```

### Step 2: Restart the app service
```bash
docker compose restart app
```
**Expected**: The app container reloads the `.env` file on restart and `settings.admin_api_key` becomes `admin_master_key`.

### Step 3: Verify the fix
```bash
curl -s -X POST http://localhost:8000/api/v1/tenants -H "Authorization: Bearer rk_test" -H "Admin-Key: admin_master_key" -H "Content-Type: application/json" -d '{"name":"verify"}'
```
**Expected output**: `{"tenant_id":"...","name":"verify","api_key":"...","plan":"shared","created_at":"...","chunk_count":0}`

### Step 4: Verify all admin routes work
```bash
# Create a key
curl -s -X POST http://localhost:8000/api/v1/verify-tenant/keys -H "Authorization: Bearer rk_verify" -H "Admin-Key: admin_master_key" -H "Content-Type: application/json" -d '{"prefix":"verify"}'

# List tenants
curl -s http://localhost:8000/api/v1/tenants -H "Authorization: Bearer rk_verify" -H "Admin-Key: admin_master_key"

# Delete a tenant (if you created one)
curl -s -X DELETE http://localhost:8000/api/v1/tenants/verify-tenant -H "Authorization: Bearer rk_verify" -H "Admin-Key: admin_master_key"
```

## Verification Checklist
- [ ] `.env` file has `ADMIN_API_KEY=admin_master_key` (not `***`)
- [ ] `docker compose exec app env | grep ADMIN_API_KEY` returns `admin_master_key`
- [ ] `POST /api/v1/tenants` with `Admin-Key: admin_master_key` returns 201
- [ ] `POST /api/v1/{tenant}/keys` with `Admin-Key: admin_master_key` returns 201
- [ ] `DELETE /api/v1/{tenant}/keys/{prefix}` with `Admin-Key: admin_master_key` returns 200
- [ ] Health endpoint `/health` still returns 200
- [ ] SLO endpoint `/health/slo` still returns availability_met: true
- [ ] Query endpoint still works with user API keys

## Related Runbooks
- `docs/circuit_breaker_open_runbook.md` - For circuit breaker tripped/open scenarios
- `docs/qdrant_unhealthy_snapshot_restore_runbook.md` - For Qdrant unhealthy/snapshot restore
- `docs/drift_replay_pass.md` - For drift between documented and actual behavior