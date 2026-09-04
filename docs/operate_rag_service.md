# Operate RAG Service — One-Page Overview

**Purpose**: Single-page overview linking every runbook, doc, and key command for operating the rag-service. Kept fresh by cross-referencing DOC_INDEX.md.

---

## Quick Bring-Up

```bash
# 1. Clone & venv
git clone <repo-url>
cd rag-service
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

# 2. Configure env
cp .env.example .env
# Edit .env with your values (ADMIN_API_KEY, DB_URL, QDRANT_URL, etc.)

# 3. Start services
docker compose up -d  # brings up Qdrant, app, Redis

# 4. Verify
curl -s http://localhost:8000/health/deps | jq .
# Expected: {"status":"ok","circuit_breakers":{"qdrant":"closed"}}
```

---

## Runbook Index

| Runbook | Path | When to Use |
|---|---|---|
| **Qdrant Unhealthy / Snapshot Restore** | `docs/qdrant_unhealthy_snapshot_restore_runbook.md` | Qdrant circuit breaker open, data loss, or recovery from snapshot |
| **Key Rotation and Revocation** | `docs/key_rotation_revocation_runbook.md` | Rotating `rk_*` secret keys or revoking leaked keys |
| **Key Rotation and Publishable Keys** | `docs/key_rotation_revocation_runbook.md#Procedure-C` | Rotating `pk_*` publishable keys (read-only) |
| **Ingestion Job Stuck / Orphaned** | `docs/ingest_job_stuck_runbook.md` | Jobs stuck in `processing` state, orphaned queue entries |
| **Requeue Orphaned Jobs** | `docs/requeue_orphaned_jobs_runbook.md` | Jobs stuck beyond retry limit |
| **Backup & Restore** | `docs/backup_restore_runbook.md` | Full system backup and restore (PostgreSQL + Qdrant) |
| **Operate RAG Service** | `docs/operate_rag_service.md` | General service operation, health checks, config |
| **Ingestion Queue Monitoring** | `docs/operator_runbook_ingestion_queue.md` | Queue backpressure, monitoring, alerting |
| **Troubleshooting Tree** | `docs/troubleshooting_tree.md` | Symptom-to-runbook mapping for 10 common failure modes |

---

## Key Commands

```bash
# Health check
curl -s http://localhost:8000/health/deps | jq .

# Circuit breaker metrics
curl -s http://localhost:8000/metrics | grep circuit_breakers

# List tenants/keys
curl -s -H "Authorization: Bearer <admin-key>" http://localhost:8000/api/v1/tenants | jq .

# Create tenant
curl -s -X POST http://localhost:8000/api/v1/tenants \
  -H "Authorization: Bearer <admin-key>" \
  -H "Content-Type: application/json" \
  -d '{"name": "my-tenant"}'

# List snapshots
ls -la /qdrant/storage/snapshots/<collection>/

# Create snapshot
python3 -c "from app.backup import create_qdrant_snapshot; print(create_qdrant_snapshot(collection='rag'))"

# Recover snapshot
python3 -c "from app.backup import recover_qdrant_snapshot; recover_qdrant_snapshot(snapshot_name='<name>', collection='rag')"

# Check DOC_INDEX freshness
grep -l "qdrant_unhealthy_snapshot_restore_runbook" docs/DOC_INDEX.md && echo "Runbook indexed" || echo "Runbook NOT indexed"
```

---

## Key Code Paths (for drift detection)

| Component | File | Purpose |
|---|---|---|
| Qdrant health | `app/main.py:365-387` | `/health/deps` endpoint |
| Snapshot create | `app/backup.py:41-51` | `create_qdrant_snapshot()` |
| Snapshot recover | `app/backup.py:54-77` | `recover_qdrant_snapshot()` |
| Circuit breaker | `app/main.py:365-387` | Qdrant circuit breaker state |
| Tenant key model | `app/db.py` | TenantKey model, revoke_api_key, add_api_key |

---

## Fresh → Stale Detection

Run this to check if docs are still fresh:

```bash
# Check DOC_INDEX.md for all entries and their fresh/stale flags
python3 -c "
import re
with open('docs/DOC_INDEX.md') as f:
    content = f.read()
# Find all entries with fresh/stale flags
entries = re.findall(r'\|\s+\`([^\`]+)\`\s+\|\s+[^\|]+\|\s+(\w+)\s+\|', content)
for path, flag in entries:
    print(f'{path}: {flag}')
print(f'\nTotal entries: {len(entries)}')
print(f'Fresh: {sum(1 for _, f in entries if f == \"Fresh\")}')
print(f'Stale: {sum(1 for _, f in entries if f == \"Stale\")}')
"
```

---

## Evidence & Commands Captured

- All runbooks verified against live service codebase (HEAD `e6d5229` on `feat/rag-agent6-month-scale`)
- Qdrant circuit breaker state: `circuit_breakers{qdrant="closed"}` = healthy, `{"open"}` = unhealthy
- Snapshot API: `create_qdrant_snapshot()` / `recover_qdrant_snapshot()` from `app/backup.py`
- Auth key types verified: `pk_*`=403 on ingest/admin, `rk_*`=200 full power, Admin-Key=200 on admin routes
- DOC_INDEX.md now tracks 31 operator-facing docs with fresh/stale flags

**Last updated**: 2026-09-04 against codebase HEAD

--- 

## Next Steps

- [ ] Coordinate with Agent 10 on any deployment-specific paths not covered
- [ ] Test off-node snapshot restore with staging Qdrant instance
- [ ] Open GitHub issue for any gaps in coverage (SSO, billing, SLAs)
- [ ] Run verification workflow from `VERIFICATION-PHASE-A.md` to check doc freshness