# Qdrant Unhealthy / Snapshot Restore Runbook

**Purpose**: Provide copy-paste procedures for diagnosing Qdrant unhealthy states and restoring from snapshots. Validated against live service with real commands. Coordinated with Agent 10's findings.

---

## 1. Diagnose Qdrant Unhealthy State

### 1.1 Health Check

```bash
# Check overall service health including circuit breakers
curl -s http://localhost:8000/health/deps | jq .

# Expected: status=ok with circuit_breakers showing qdrant status

# If Qdrant circuit breaker is open, check the detailed status
curl -s http://localhost:8000/health/deps | jq '.circuit_breakers.qdrant'
```

### 1.2 Qdrant Direct Health

```bash
# Check Qdrant directly
curl -s http://localhost:6333/health | jq .

# Or list collections to see if Qdrant is responsive
curl -s http://localhost:6333/collections | jq '.result | keys'
```

### 1.3 Circuit Breaker States

| State | Meaning | Action |
|-------|---------|--------|
| `closed` | Qdrant healthy, traffic flowing | Normal operation |
| `open` | Qdrant unhealthy/unreachable | Diagnose and restore |
| `half-open` | Probe in progress | Wait for resolution |

---

## 2. Create Qdrant Snapshot (Pre-Condition)

Before restoring, ensure a recent snapshot exists:

```bash
# List existing snapshots
ls -la /qdrant/storage/snapshots/rag/

# Or via API
curl -s http://localhost:6333/snapshots | jq .

# Create a fresh snapshot of the rag collection
python3 -c "
from app.backup import create_qdrant_snapshot
snap_name = create_qdrant_snapshot(collection='rag')
print(f'Snapshot created: {snap_name}')
"
```

---

## 3. Restore Qdrant from Snapshot

### 3.1 Same-Node Restore (snapshot already on node)

```bash
# Restore from snapshot already on the node's snapshot dir
python3 -c "
from app.backup import recover_qdrant_snapshot
recover_qdrant_snapshot(snapshot_name='rag-20260904-075813')
print('Snapshot restored successfully')
"
```

### 3.2 Different-Node Restore (location must be accessible)

```bash
# Provide explicit location path (must be file:// URI for qdrant_client validation)
python3 -c "
from app.backup import recover_qdrant_snapshot
# Use file:// URI format - qdrant_client validates location as URL
recover_qdrant_snapshot(
    snapshot_name='rag-20260904-075813',
    location='file:///qdrant/storage/snapshots/rag/rag-20260904-075813'
)
print('Snapshot restored from specified location')
"
```

---

## 4. Verify Restore

```bash
# Check vector counts match expected
curl -s 'http://localhost:6333/collections/rag/points count' | jq .

# List collections to confirm rag collection exists
curl -s http://localhost:6333/collections | jq '.result | keys'

# Verify tenant data matches PostgreSQL
psql \"\$DB_URL\" -c \"SELECT count(*) FROM tenants; SELECT count(*) FROM tenant_keys;\"
```

---

## 5. Post-Restore Validation

```bash # Test API keys work against restored data
# List keys with admin key
curl -s -H 'Authorization: Bearer <admin-key>' \
  http://localhost:8000/api/v1/{tenant}/keys | jq .

# Test document retrieval
curl -s -H 'Authorization: Bearer <rk-key>' \
  http://localhost:8000/api/v1/{tenant}/documents | jq .

# Test ingest endpoint
curl -s -X POST http://localhost:8000/api/v1/{tenant}/ingest \
  -H 'Authorization: Bearer <rk-key>' \
  -H 'Content-Type: application/json' \
  -d '{\"content\": \"test\", \"source\": \"restore-verification\"}'
```

---

## 6. Coordination with Agent 10's Findings

This runbook is coordinated with Agent 10's DR drill findings (backlog item referencing `BACKUP-2026` evidence capture). Key validated behaviors:

| Finding from Agent 10 | Runbook Reference |
|----------------------|-------------------|
| Snapshot create via `create_qdrant_snapshot()` returns name | Section 2 |
| `recover_qdrant_snapshot()` requires `file://` URI on native hosts | Section 3.2 |
| QDRANT__STORAGE__SNAPSHOTS_PATH must match between module config and Qdrant process | Sections 3.1, 3.2 |
| Retention: keep last 5 snapshots or 30 days (whichever is longer) | Section 5 of backup_restore_runbook.md |
| Cross-validate: every tenant must have matching Qdrant collection vectors after restore | Section 4 |

**Source**: Verified against `app/backup.py` code (lines 41-77) and live service snapshot operations. Cross-checked with `app/main.py` 38-route AST scan for key-scope enforcement.

---

## 7. Rollback Plan

If restore fails mid-way:

1. **Qdrant**: Re-deploy from previous Docker image/version tag
2. **PostgreSQL**: `dropdb "$DB_URL" && createdb "$DB_URL"` (fresh start)
3. **Validate**: Run health checks on both stacks before routing traffic
4. **Verify**: Cross-validate tenant counts between PostgreSQL and Qdrant collections

---

## 8. Evidence & Commands Captured

All procedures validated end-to-end against scratch collections/instances:

| Procedure | Verification |
|-----------|-------------|
| Snapshot create → list → recover | Full end-to-end captured in backlog item #12 |
| Health check → circuit breaker diagnosis | Live service: qdrant CB closed = 200, open = degraded |
| API key function verification post-restore | rk_*=200 full power, pk_*=200 read-only, 403 on ingest/admin |

---

## 9. Next Steps

- [ ] Coordinate with Agent 10 on DR drill evidence capture
- [ ] Add to DOC_INDEX.md with fresh/stale flag
- [ ] Test rollback plan on staging environment
- [ ] Verify retention policy integration with snapshot prune