# Operate RAG Service

## Health Checks

### Service Health
```bash
curl -s -H "Admin-Key: admin_master_key" http://localhost:8000/health
# Expected: {"status":"ok","service":"rag-service","version":"1.0.0"}
```

### SLO Metrics
```bash
curl -s -H "Admin-Key: admin_master_key" http://localhost:8000/health/slo
# Expected: availability 0.9996
```

### Readiness
```bash
curl -s -H "Admin-Key: admin_master_key" http://localhost:8000/health/liveness
# Expected: service is ready to serve requests
```

## Admin Operations

### Create a Tenant
```bash
curl -s -X POST "http://localhost:8000/api/v1/tenants" \
  -H "Admin-Key: admin_master_key" \
  -H "Content-Type: application/json" \
  -d '{"name":"my-tenant"}'
# Expected: 201 {"tenant_id":"my-tenant","name":"my-tenant","api_key":"rk_...","plan":"shared","created_at":"...","chunk_count":0}
```

### Rotate/Create Secret Key
```bash
curl -s -X POST "http://localhost:8000/api/v1/keys" \
  -H "Admin-Key: admin_master_key" \
  -H "Content-Type: application/json" \
  -d '{"prefix":"my-key"}'
# Expected: 201 {"doc_id":"...","key":"rk_...","created_at":"..."}
```

## Query Operations

### First Query with Secret Key
```bash
# Use the secret key from tenant creation or key create response
curl -s -X POST "http://localhost:8000/api/v1/query" \
  -H "Authorization: Bearer rk_..." \
  -H "Content-Type: application/json" \
  -d '{"question":"What is this service?", "mode": "dense"}'
# Expected: 200 {"answer":"...","citations":[...],"mode":"dense","model":"...","usage":{...}}
```

### Query with Publishable Key (read-only)
```bash
curl -s -X POST "http://localhost:8000/api/v1/query" \
  -H "Authorization: Bearer pk_..." \
  -H "Content-Type: application/json" \
  -d '{"question":"test"}'
# Expected: varies — read-only access, may return limited results
```

## Common Failure Modes

### Admin-Key on Query Endpoint
```bash
curl -s -X POST "http://localhost:8000/api/v1/query" \
  -H "Admin-Key: admin_master_key" \
  -H "Content-Type: application/json" \
  -d '{"question":"test"}'
# Expected: 403 {"detail":"Admin key required"}
```

### Bearer Auth on Admin Endpoint
```bash
curl -s -X POST "http://localhost:8000/api/v1/tenants" \
  -H "Authorization: Bearer pk_..." \
  -H "Content-Type: application/json" \
  -d '{"name":"test"}'
# Expected: 403 {"detail":"Admin key required"}
```

### Invalid/Expired Key
```bash
curl -s -X POST "http://localhost:8000/api/v1/query" \
  -H "Authorization: Bearer invalid-key" \
  -H "Content-Type: application/json" \
  -d '{"question":"test"}'
# Expected: 401 {"detail":"Invalid API key"}
```
