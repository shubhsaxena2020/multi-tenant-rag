# Release Readiness Checklist (v17.04 / e6d5229)

**Purpose**: One-page operator sign-off before promoting a tagged release to production. Covers migrations, secrets, smoke tests, rollback, dashboards, and tenant-isolation verification.

---

## 1. Code / Migration

- [ ] All code changes merged to `feat/rag-agent6-month-scale` via PR → squash merge → tag
- [ ] No uncommitted modified files (verify `git status --short` is clean except new files)
- [ ] DB migrations tested: `alembic upgrade head` or equivalent; no schema drift
- [ ] Sealed secrets / API keys rotated and present in `.env` (check `MASTER_ENCRYPTION_KEY`, `ADMIN_API_KEY`)
- [ ] No `print()` or `logging` of secrets in code or test output

## 2. Vector Store / Tenant Isolation

- [ ] Vector store mode confirmed: Pool (shared collection + tenant_id payload filter) — verify `app/vector_store.py` line 2
- [ ] `tenant_id` payload index with `is_tenant=true` is active (Qdrant payload schema)
- [ ] Fail-closed path-tenant vs key-tenant validation working (Issue #15 in `app/main.py`)
- [ ] No per-tenant structural isolation claims in public docs (verified against `README.md`)

## 3. Smoke Tests (run with project venv)

```
source .venv/bin/activate
pytest tests/ -x --timeout=120  [at least 228/235 must pass]
```

- [ ] `pytest` runs from project venv; SQLAlchemy available
- [ ] Health check: `GET /health` → 200
- [ ] Create tenant: `POST /api/v1/tenants` → 201
- [ ] Ingest text: `POST /{tenant}/ingest/text` → job created, progress tracked
- [ ] Query with key: `POST /{tenant}/query` with `Authorization: Bearer <pk_or_rk_key>`
- [ ] Read-only publishable key (`pk_*`) works for queries; secret key (`rk_*`) rejected from widget embedding

## 4. Dashboards / Observability

- [ ] Prometheus metrics endpoint: `GET /metrics` → structured JSON output
- [ ] Grafana dashboards loaded from `deploy/grafana/dashboards/rag-quality.json`
- [ ] Alert rules from `deploy/alert.rules.yml` are syntactically valid
- [ ] Qdrant health: `GET /api/status` from compose (or embedded mode)

## 5. Rollback

## 8. Release-Evidence Automation

- [ ] Wire one manual release-evidence step (smoke output, dashboard snapshot, migration proof) into a script. Done when: `scripts/release_evidence.sh` produces the artifact in one command.
- [ ] Add the release-evidence step to the RELEASE-CHECKLIST. Done when: the checklist references the script and an operator can follow it without tribal knowledge.

- [ ] Tag present in git: `git tag --contains <tag>` confirms release milestone
- [ ] Previous tag accessible for `git checkout <old_tag> && make restart` or docker-compose down/up
- [ ] Backup of `rag_tenants.db` and `qdrant_storage` before upgrade

## 6. Tenant-Isolation Verification

- [ ] Cross-tenant read is impossible at storage layer (verify vector_store.py tenant_id filter)
- [ ] Path-tenant mismatch is fail-closed (verified in main.py Issue #15)
- [ ] Publishable key (`pk_*`) resolves to same tenant_id but is scope-locked to query endpoints
- [ ] Secret key (`rk_*`) has full power — never embed in client-side widget

## 7. Final Sign-Off

- [ ] All items above checked
- [ ] Release tag created: `batch-1-phase2-quality-improvements` (or appropriate)
- [ ] CHANGELOG.md entry added for the release

---
**Tag**: v17.04 (e6d5229)
**Audited**: 2026-09-01