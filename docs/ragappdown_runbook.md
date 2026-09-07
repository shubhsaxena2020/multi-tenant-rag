# Runbook: RAG Service Admin Key Configuration (RAGAppDown / Service-Down Case)

## Overview
This runbook documents the failure mode where admin routes return `403 "Admin key required"` because the `.env` file has a placeholder `ADMIN_API_KEY` value instead of the real `admin_master_key`. When `ADMIN_API_KEY` is not configured correctly, all admin operations (tenant create, key rotation, revocation, offboard) fail-closed.

## Symptoms
- `POST /api/v1/tenants` returns `403 {"detail":"Admin key required"}` even with valid `Admin-Key` header
- `POST /api/v1/{tenant}/keys` returns `403 {"detail":"Admin key required"}`
- `DELETE /api/v1/{tenant}/keys/{prefix}` returns `403 {"detail":"Admin key required"}`
- `GET /health/slo` may still return 200 (health check is not admin-gated)
- All audit routes (`/audit`, `/audit/verify`) fail-closed without the Admin-Key

## Root Cause
The `.env` file contains `ADMIN_API_KEY=***` (placeholder) instead of `ADMIN_API_KEY=admin_master_key`. The service's `require_admin()` guard in `app/auth.py` checks `settings.admin_api_key` against the provided `Admin-Key` header value. Without a matching value, all admin routes return 403.

## Fix: Update `.env` with correct `ADMIN_API_KEY`

### Step 1: Verify current `.env` value
```bash
cat /home/ubuntu/rag-service/.env | grep ADMIN_API_KEY
```
**Expected output**: `ADMIN_API_KEY=***`

### Step 2: Fix the `.env` file
Replace the placeholder with the real admin master key:
```bash
sed -i 's/^ADMIN_API_KEY=.*$/ADMIN_API_KEY=admin_master_key/' /home/ubuntu/rag-service/.env
```

### Step 3: Verify the fix
```bash
cat /home/ubuntu/rag-service/.env | grep ADMIN_API_KEY
```
**Expected output**: `ADMIN_API_KEY=admin_master_key`

### Step 4: Restart the app container to pick up the new env var
```bash
cd /home/ubuntu/rag-service
docker compose restart app
```

### Step 5: Verify admin routes now work
```bash
# Test tenant create (was returning 403, should now return 201)
curl -s -X POST "http://localhost:8000/api/v1/tenants" \
  -H "Authorization: Bearer rk_*" \
  -H "Admin-Key: admin_master_key" \
  -H "Content-Type: application/json" \
  -d '{"name":"verify-tenant"}'

# Expected: {"tenant_id":"verify-tenant","name":"verify-tenant","api_key":"rk_...","plan":"shared","created_at":"...","chunk_count":0} (201)
```

```bash
# Test key issue (was returning 403, should now return 201)
curl -s -X POST "http://localhost:8000/api/v1/verify-tenant/keys" \
  -H "Authorization: Bearer rk_*" \
  -H "Admin-Key: admin_master_key" \
  -H "Content-Type: application/json" \
  -d '{"prefix":"verify-key"}'

# Expected: {"doc_id":"...","key":"rk_...","created_at":"..."} (201)
```

```bash
# Test health SLO (should still work)
curl -s http://localhost:8000/health/slo

# Expected: {"availability":0.9996,"availability_target":0.99,"availability_met":true,...}
```

## Expected Output After Fix

### Tenant Create (201)
```json
{
  "tenant_id": "verify-tenant",
  "name": "verify-tenant",
  "api_key": "rk_...",
  "plan": "shared",
  "created_at": "...",
  "chunk_count": 0
}
```

### Key Issue (201)
```json
{
  "doc_id": "...",
  "key": "rk_...",
  "created_at": "..."
}
```

### Health SLO (200)
```json
{
  "availability": 0.9996,
  "availability_target": 0.99,
  "availability_met": true,
  "latency_p95_s": 0.0,
  "latency_target_s": 0.5,
  "latency_met": true,
  "total_requests": 8147,
  "status": "ok"
}
```

## Prevention
- Ensure `.env` is never committed with `ADMIN_API_KEY=***` as a placeholder
- The `.env.bak` file contains the correct value `ADMIN_API_KEY=admin_master_key` as a reference
- Add `ADMIN_API_KEY` validation to CI/CD pipeline to catch missing/placeholder values before deployment
- Document in onboarding that `ADMIN_API_KEY=admin_master_key` must be set in `.env` before first start

## Linked Resources
- `docs/circuit_breaker_open_runbook.md` - for service restart/circuit breaker scenarios
- `docs/operate_rag_service.md` - overview of all runbooks
- `Dockerfile` - for building the app image
- `deploy/prometheus.yml` - monitoring config for detecting admin key issues