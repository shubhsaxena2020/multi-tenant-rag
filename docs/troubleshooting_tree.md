# Top-Level Troubleshooting Tree (RAG App Operator Guide)

**Purpose**: One-page reference covering the 8 most common operator failure modes for the Hermes `rag-service`, with symptom -> likely cause -> runbook link mapping. All references are to real code paths in the active codebase.

---

## Failure Mode Matrix

| # | Symptom | Likely Cause | Runbook |
|---|---|---|---|
| 1 | Ingestion job stuck in `running` > 30 min | Worker crash; ThreadPoolExecutor exhausted; job never submitted | `docs/ingest_job_stuck_runbook.md` |
| 2 | `403 Forbidden` on ingest with `pk_*` key | Publishable key used for write operation (expected per scope rules) | `docs/AUTH-SCOPE-VERIFICATION.md` |
| 3 | `401 Unauthorized` on all routes | Key revoked; key expired (past expiry); key not found in DB | `docs/key_rotation_revocation_runbook.md` |
| 4 | `403 Forbidden` on admin routes with `rk_*` key | Wrong key tier; key missing admin scope; path-based tenant check failed | `docs/AUTH-SCOPE-VERIFICATION.md` |
| 5 | Qdrant search returns no results | Collection missing; tenant_id payload filter not indexed; embedding mismatch | Check `app/vector_store.py:ensure_collection()` and vector size config |
| 6 | Service won't start (`docker compose up -d`) | Missing env vars; port conflicts; DB file permissions | `docs/deployment.md` verification already done (item 9) |
| 7 | API rate-limit exceeded (429) | `rate_limit_rpm` or `ingest_rate_limit_rpm` quota exhausted | Check tenant config in DB; rotate/regenerate keys if needed |
| 8 | Job queue stuck (inline mode, single-replica) | ThreadPoolExecutor at max workers (4); Redis not configured for multi-replica | `app/ingestion/runner.py:_is_redis_backend()` — set `REDIS_URL` env var |
| 9 | Expired key suddenly rejected (401) | `expires_at` timestamp reached; per v10.8 behavior: expired = rejected like revoked | Rotate key or set `expires_at` to future value via admin API |
| 10 | Publishable key used for rotate/revoke (403) | Per P1 #9: `pk_*` keys cannot rotate or revoke other keys; only `rk_*` full-power keys | See `docs/key_rotation_revocation_runbook.md` Procedure C |

---

## Quick-Decision Flow: "What's broken?"

### Step 1: Identify the error code

| Error | Most Likely Cause | First Action |
|---|---|---|
| `401 Unauthorized` | Key revoked, expired, or not found | Check key status: `GET /{tenant}/keys` with Admin-Key |
| `403 Forbidden` | Key tier mismatch (pk_* on admin, rk_* wrong route) | Verify key kind and intended route |
| `500 Internal` | Server error (DB, Qdrant, embedding) | Check server logs; verify Qdrant connectivity |
| `503 Service Unavailable` | Queue back end down (Redis) or worker exhausted | Check Redis connectivity; restart worker |
| `429 Too Many Requests` | Rate limit exceeded | Back off; check tenant rate_limit_rpm config |

### Step 2: Match to symptom category

Use the matrix above to find your symptom number, then follow the linked runbook.

### Step 3: Execute recovery

Each runbook provides copy-paste commands verified against the real codebase. All procedures:
- Reference actual `app/*.py` code paths
- Use real API endpoints from the running service
- Include expected HTTP response codes
- Are safe to run against a local bring-up (no production risk)

---

## Evidence & Code References

| Runbook | Code Source | Verified Against |
|---|---|---|
| `docs/ingest_job_stuck_runbook.md` | `app/ingestion/runner.py:submit()`, `app/jobs.py:update_job()` | Live service: job states (pending/running/completed/failed) |
| `docs/AUTH-SCOPE-VERIFICATION.md` | `app/main.py:38` route AST scan; key-tier enforcement | Live service: pk_*=403 on ingest/admin, rk_*=200 full power |
| `docs/key_rotation_revocation_runbook.md` | `app/db.py:TenantKey`, `revoke_api_key()`, `add_api_key()` | Live service: pk_* cannot rotate/revoke, rk_* can |
| `docs/deployment.md` | `docker-compose.yml` env vars; `.env.example` | Clean box bring-up: all env vars match |

---

## Prevention Checklist (run monthly)

- [ ] Verify all docs in `DOC_INDEX.md` have `Fresh` or `Stale` flag
- [ ] Check no `pk_*` key has admin privileges (audit `app/db.py:TenantKey.kind`)
- [ ] Confirm `REDIS_URL` is set if running > 1 replica (prevents item #8)
- [ ] Review key expiry dates (ensure no keys approaching past expiry)
- [ ] Validate Qdrant collection payload indexes (`tenant_id` with `is_tenant=True`)
- [ ] Run `python -m pytest tests/test_key_scope_verification.py -x` to verify auth rules
- [ ] Run `python -m pytest tests/test_security_fixes.py -x` to verify key expiry/rotation

---

## Current Behavior vs Planned Behavior

| Feature | Current Behavior (v10.8) | Planned Behavior (future Phase) |
|---|---|---|
| Key expiry enforcement | Keys with past `expires_at` rejected as 401; keys with future `expires_at` still valid | Full key rotation lifecycle: auto-expire, auto-revoke, email notification to tenant admin, grace period before key becomes read-only |
| Publishable key scope | `pk_*` keys cannot rotate/revoke other keys (403); read-only by design | Publishable keys will gain scoped capabilities per plan tier (standard/pro/enterprise) with fine-grained route permissions |
| Key rotation frequency | Manual via admin API; no automated rotation | Scheduled key rotation (configurable interval) with zero-downtime handover; old key remains valid until new key propagates |
| Multi-hop retrieval | Standard tenants clamped to 1 hop; enterprise allows >1 hop | Plan-gated ceilings will be uniformly enforced at API boundary via `app/plans.py:capabilities_for()` with 402 responses for over-quota requests |

**Source**: Plan capabilities defined in `app/plans.py:28-38`; multi-hop gate in `app/retrieval/agentic.py:11,27`; route enforcement in `app/main.py:1367-1384` and `app/auth.py:26` (key-tier derivation). Verified against live service and test suite.

---",

---

**Last updated**: 2026-09-04 against codebase HEAD `57a3cfd` on `feat/rag-agent6-month-scale`
**Total failure modes covered**: 10 (the 8 most common + 2 bonus from adjacent items)