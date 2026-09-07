# DOC INDEX — Operator-Facing Documentation Inventory

This file catalogs every operator-facing document in the rag-service repo, with a one-line purpose and a fresh/stale flag indicating whether the doc accurately reflects the current codebase state.

| Doc Path | One-Line Purpose | Fresh / Stale |
|---|---|---|
| `README.md` | Full service overview: architecture, API, deployment, health checks, rate limits, observability | Fresh (updated alongside v3 code; all code blocks verified against live service) |
| `ONBOARDING.md` | Step-by-step: tenant creation → key issue → ingest → first query via SDK/curl/admin console | Stale (curl examples use old `sk_` admin-key pattern; SDK `RagClient` flow is the current path; some admin-console steps diverge from actual API) |
| `docs/operator_runbook_ingestion_queue.md` | Runbook for ingestion queue monitoring and backpressure handling | Fresh (matches real queue code paths; no drift detected) |
| `docs/requeue_orphaned_jobs_runbook.md` | Runbook for requeueing orphaned/stuck ingestion jobs | Stale (references job ID format that changed in Phase K; needs update for new `job_id` schema) |
| `DEPLOYMENT.md` | Local/VPS self-host via docker-compose; brings up Qdrant, app, Redis | Fresh (compose file and env vars in sync with current HEAD) |
| `ISOLATION.md` | Tenant isolation strategy: silo pattern (per-tenant Qdrant collections) | Fresh (structural isolation matches code; audit-verifiable) |
| `ENV-ASSUMPTIONS-NOTE.md` | Verified runtime env config from HEAD audit; .env values and production assumptions | Fresh (audited against actual `.env` at HEAD; all flags verified) |
| `BRANCH-TAG-HYGIENE.md` | Branch/tag governance; tag inventory and branch hygiene report | Stale (tag list last updated at prior milestone; new batch tags may be missing) |
| `DESIGN-TRADEOFFS.md` | Architecture trade-off decisions with citations (Qdrant tenancy, embedding, chunking, etc.) | Fresh (cited sources verified 2026; cross-checked with code) |
| `VERIFICATION-PHASE-A.md` | Phase A independent verification report (CORS + key-tier split) | Fresh (verification ran against live service; held up with no gaps) |
|| `TENANT-PATH-VERIFICATION.md` | Tenant path semantics & key-scope enforcement (fail-closed path tenant matching) | Fresh (verified against `app/main.py` 38-route AST scan; pk_* scope rules verified against live service with example curl for each key type: pk_*=403 on ingest/admin, rk_*=200 full power, Admin-Key=200 on admin routes) |
| `ingest_job_stuck_runbook.md` | Ingestion job stuck / orphaned recovery runbook with copy-paste recovery sequences and code-path references | Fresh (references real `app/ingestion/runner.py` and `app/jobs.py` code paths; verified against live service) |
| `key_rotation_revocation_runbook.md` | Key rotation and revocation runbook with copy-paste sequences and code-path references to `app/db.py` | Fresh (references real `app/db.py` TenantKey model, revoke_api_key, add_api_key; verified against live service: pk_* cannot rotate/revoke, rk_* can) |
| `troubleshooting_tree.md` | Top-level troubleshooting tree (symptom -> likely cause -> runbook link) covering 10 most common operator failure modes with matrix and quick-decision flow | Fresh (references all above runbooks + real code paths across `app/*.py`; verified against live service) |
| `CHANGELOG.md` | Project changelog — version history | Stale (covers up to v17.04; incremental changes since not yet entered) |
| `BACKLOG.md` | High-level backlog / project overview | Stale (last refreshed at batch start; new items added since) |
| `NEEDS_HUMAN.md` | Flags requiring product/operator input or external decisions | Fresh (current state accurate; no drift) |
| `RELEASE-CHECKLIST.md` | Release readiness checklist | Stale (frozen at prior release; new checklist items pending) |
| `RELEASE_EVIDENCE.md` | Release verification evidence and test results | Stale (covers tests passed at prior milestone) |
| `TASK6_ORPHAN_WORKER_SCALING.md` | Task-specific worker scaling analysis | Stale (task closed; content archived but not updated) |
| `TASK8_VECTOR_QUOTA_TRIAGE.md` | Task-specific vector quota triage analysis | Stale (task closed; content archived but not updated) |
| `staging_plan.md` | Staging deployment plan and pre-prod validation steps | Stale (plan from prior cycle; staging env may have drifted) |
| `target_checklist.md` | Target/goal checklist for the current phase | Stale (reflects targets at branch creation; new goals added since) |
| `sdk-js/README.js` (js dir) | JavaScript SDK reference and usage | Fresh (SDK aligned with v3 API contract) |

**Legend:** Fresh = doc content verified accurate against current codebase (HEAD `e6d5229` on `feat/rag-agent6-month-scale`). Stale = doc known to drift from code, missing recent changes, or archived from a prior cycle.

**How to mark a doc Fresh → Stale or vice versa:** Run the operator-verification workflow (see `VERIFICATION-PHASE-A.md` methods) or cross-check doc claims against the living codebase. When in doubt, err on Stale and file a follow-up task.