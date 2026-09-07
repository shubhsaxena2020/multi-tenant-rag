# Onboarding Friction Pass: Tenant-Create to First-Query Path

## Overview
This runbook walks the end-to-end path from creating a new tenant to making your first document query, identifying and resolving the single worst confusion point along the way.

## The Single Worst Confusion Point
**Admin-Key vs Bearer Token authentication mismatch.**

New operators frequently attempt to use the `Admin-Key: admin_master_key` header on query endpoints, or use `Authorization: Bearer <publishable_key>` on admin endpoints. Both patterns fail with `403 {"detail":"Admin key required"}` or `401 {"detail":"Invalid API key"}`, respectively.

**The rule of thumb:**
- `Admin-Key: admin_master_key` → admin operations (tenant/create, key rotation, revocation)
- `Authorization: Bearer <secret_key>` → query operations (document search, chat)
- `Authorization: Bearer <publishable_key>` → read-only queries (safe to embed client-side)

## Step-by-Step Path

### Step 1: Verify .env has correct ADMIN_API_KEY
```bash
cd /home/ubuntu/rag-service
cat .env | grep ADMIN_API_KEY
# Expected: ADMIN_API_KEY=admin_master_key
# If you see ADMIN_API_KEY=***, it's a placeholder — update it:
sed -i 's/^ADMIN_API_KEY=.*$/ADMIN_API_KEY=admin_master_key/' .env
```

### Step 2: Verify service health
```bash
curl -s http://localhost:8000/health
# Expected: {"status":"ok","service":"rag-service","version":"1.0.0"}
```

### Step 3: Create a tenant (admin operation)
**Authentication: `Admin-Key: admin_master_key` header**

```bash
curl -s -X POST "http://localhost:8000/api/v1/tenants" \
  -H "Admin-Key: admin_master_key" \
  -H "Content-Type: application/json" \
  -d '{"name":"my-first-tenant"}'
# Expected: {"tenant_id":"my-first-tenant","name":"my-first-tenant","api_key":"rk_...","plan":"shared","created_at":"...","chunk_count":0} (201)
```

**Common mistake:** Using `Authorization: Bearer ***` instead of `Admin-Key: admin_master_key` → returns `403 {"detail":"Admin key required"}`

### Step 4: Create a secret key for the tenant (admin operation)
**Authentication: `Admin-Key: admin_master_key` header**

```bash
curl -s -X POST "http://localhost:8000/api/v1/keys" \
  -H "Admin-Key: admin_master_key" \
  -H "Content-Type: application/json" \
  -d '{"prefix":"my-key"}'
# Expected: {"doc_id":"...","key":"rk_...","created_at":"..."} (201)
```

### Step 5: Make your first query (user operation)
**Authentication: `Authorization: Bearer <secret_key>` header**

```bash
# First, get the secret key from tenant creation response, or create one:
curl -s "http://localhost:8000/api/v1/keys" \
  -H "Authorization: Bearer admin_master_key" \
  -H "Admin-Key: admin_master_key"

# Then use the secret key to query:
curl -s -X POST "http://localhost:8000/api/v1/query" \
  -H "Authorization: Bearer rk_..." \
  -H "Content-Type: application/json" \
  -d '{"question":"What is the capital of France?", "mode": "dense"}'
# Expected: {"answer":"Paris...", "citations": [...], "mode": "dense", "model": "...", "usage": {...}} (200)
```

**Common mistake #1:** Using `Admin-Key: admin_master_key` on query endpoint → `403 {"detail":"Admin key required"}`
**Common mistake #2:** Using `Authorization: Bearer pk_...` (publishable key) on query → may return limited results or `401`
**Common mistake #3:** Using `Authorization: Bearer ***` without a valid key → `401 {"detail":"Invalid API key"}`

## Summary: Auth Mechanism Quick Reference

| Operation | Header | Example | Failure if wrong |
|---|---|---|---|
| Tenant create | `Admin-Key: admin_master_key` | `curl -s -X POST ... -H "Admin-Key: admin_master_key"` | `403 Admin key required` |
| Key create | `Admin-Key: admin_master_key` | `curl -s -X POST ... -H "Admin-Key: admin_master_key"` | `403 Admin key required` |
| Document query | `Authorization: Bearer rk_...` | `curl -s -X POST ... -H "Authorization: Bearer rk_..."` | `401 Invalid API key` |
| Publishable query | `Authorization: Bearer pk_...` | `curl -s -X POST ... -H "Authorization: Bearer pk_..."` | Limited/No results |

## Reproducible Transcript

### Before Fix (confused auth):
```bash
# Anti-pattern: Using Admin-Key on query endpoint
curl -s -X POST "http://localhost:8000/api/v1/query" \
  -H "Admin-Key: admin_master_key" \
  -H "Content-Type: application/json" \
  -d '{"question":"test"}'
# Output: 403 {"detail":"Admin key required"}

# Anti-pattern: Using Bearer publishable key on admin endpoint  
curl -s -X POST "http://localhost:8000/api/v1/tenants" \
  -H "Authorization: Bearer pk_..." \
  -H "Content-Type: application/json" \
  -d '{"name":"test"}'
# Output: 403 {"detail":"Admin key required"}
```

### After Fix (correct auth):
```bash
# Correct: Admin-Key on admin endpoint
curl -s -X POST "http://localhost:8000/api/v1/tenants" \
  -H "Admin-Key: admin_master_key" \
  -H "Content-Type: application/json" \
  -d '{"name":"my-tenant"}'
# Output: 201 {"tenant_id":"my-tenant",...}

# Correct: Bearer secret key on query endpoint
curl -s -X POST "http://localhost:8000/api/v1/query" \
  -H "Authorization: Bearer rk_..." \
  -H "Content-Type: application/json" \
  -d '{"question":"test"}'
# Output: 200 {"answer":"...","citations":[...],...}
```

## Checklist: Tenant-Create to First-Query

- [ ] `.env` has `ADMIN_API_KEY=admin_master_key` (not `***`)
- [ ] Service health: `curl -s http://localhost:8000/health` → 200
- [ ] Tenant create: `curl -s -X POST ... -H "Admin-Key: admin_master_key"` → 201
- [ ] Secret key available from tenant response or key create endpoint
- [ ] First query: `curl -s -X POST ... -H "Authorization: Bearer rk_..."` → 200
- [ ] Query returns answer with citations

## Key Takeaway
The most important onboarding concept is **separating admin authentication from user authentication**:
- Admin operations always use `Admin-Key: admin_master_key`
- User queries always use `Authorization: Bearer <secret_key>` (never `pk_` publishable keys for full queries)
- Never mix these two mechanisms
<tool_call>
