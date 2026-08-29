# RAG Service — Research Backlog (non-urgent findings)

Collected from the two independent 2026 deep-research passes (ops/scale-readiness +
product/client-readiness), grounded in the actual code. Items addressed in v9 are marked
[DONE]; remaining items are lower-priority / need product input. Each carries a concrete
next step so it isn't a dead note.

## Ops / Scale-readiness

- [DONE] **Qdrant incident**: single-point HTTP calls had no retry/timeout → native
  server-side hybrid fusion (prefetch + RRF/DBSF) in app/vector_store.py, plus resiliency
  (v9-1). Next: add a read-replica Qdrant / horizontal sharding config; document runbook.
- [DONE] **Graceful degradation**: query path now returns `degraded:true` instead of 500s;
  circuit breaker + retry + reranker fallback. Next: expose breaker state in /health/deps
  (already done) and wire an Alertmanager receiver.
- [DONE] **Backup & DR**: Qdrant snapshots API + SQLite/PG backup + KMS key versioning with
  non-breaking rotation. Next: schedule nightly snapshots via cron + offsite copy; run the DR
  drill in CI against a real Qdrant container (currently skipped under :memory:).
  STATUS (v8.1.1): added deploy/native/nightly_backup.sh (snapshots all collections + metadata
  backup, optional offsite copy, local retention, cron line included). Ran LIVE: snapshots `rag`
  + SQLite backup produced (exit 0). Fixed a real recover bug in app/backup.py: recover_snapshot
  `location` must be a `file://` URI (bare leading-slash path is rejected by qdrant_client as
  "relative URL without a base"); documented that native hosts MUST set QDRANT__STORAGE__SNAPSHOTS_PATH
  to a real server-readable dir AND point config qdrant_snapshot_dir at the same path, or recover
  reports "Snapshot file ... not found". The live VPS qdrant currently has 4 `rag` snapshots,
  confirming create works; recover on this host is gated on the deploy fixing the snapshots path.
- [ ] **Horizontal scaling of ingestion workers**: the in-memory job queue (app/db.py jobs)
  is durable but single-replica. For N replicas, front with an at-least-once queue
  (Cloud Tasks / RQ / Celery) that calls the existing runner. Documented in requeue_orphaned_jobs().
  Next: add a `JOB_QUEUE` backend setting + a Cloud Tasks enqueue wrapper.
- [ ] **Per-tenant rate limiting / quotas**: rate_limit() exists but is global-ish; add
  per-tenant quotas (requests/min, chunks ingested, collection size) to prevent noisy
  neighbors. Next: store quota counters in the tenant registry; return 429 with Retry-After.
- [ ] **Connection pooling / worker tuning**: verify uvicorn workers × Qdrant pool sizing
  under load. Next: load-test with k6/locust; tune `workers` and `qdrant_timeout`.
- [DONE] **Structured metrics cardinality audit**: grep of every `.labels()` call in app/ confirms
  NO unbounded labels — all use bounded values (INGEST_JOBS status set, SLO_AVAILABILITY
  outcome ok/error, DEGRADED_RESPONSES/REQUEST_* use the NORMALIZED route template `label_path`,
  CIRCUIT_OPEN_EVENTS uses fixed dependency set). tenant_id is never a metric label. v8 #5 verified.
- [ ] **Horizontal scaling of ingestion workers**: the in-memory job queue (app/db.py jobs)

## Product / Client-readiness

- [DONE] **Embeddable client widget + SDK + SSE**: app/static/widget.{js,html}, sdk.py,
  POST /api/v1/{tenant}/query/stream. Next: add a hosted demo + npm package for the SDK;
  add CORS policy config for the stream endpoint.
- [DONE] **Continuous eval & answer quality**: self-hosted LLM-as-judge (faithfulness +
  answer_relevancy), trend history, golden auto-gen. Next: build a Grafana panel over
  eval_runs; add scheduled nightly eval; add more metrics (context_precision, answer_correctness).
- [DONE] **SLOs/alerting**: /health/slo + deploy/alert.rules.yml + a Grafana dashboard JSON
  (deploy/grafana/dashboards/rag-svc.json) + a NATIVE Prometheus/Alertmanager/webhook deploy
  (deploy/native/, no Docker needed). Prometheus :9090 / Alertmanager :9093 / webhook :9099 are
  LIVE on this VPS; rag-app target shows DOWN (port 8007 not yet restarted) and RAGAppDown fires
  + is delivered to /home/ubuntu/monitoring/rag-alerts.log — P0 detect-within-minutes verified.
- [ ] **Multi-language / i18n**: widget + answers assume English. Next: detect query lang,
  pick embedder/LLM accordingly, localize widget copy.
- [ ] **Conversation memory UI**: session store exists (get_session_store) but the widget
  doesn't yet render multi-turn history or show sources inline with citations. Next: pass
  session_id from widget; render cited chunks.
- [ ] **Admin console**: operators manage tenants/keys/eval via API only. Next: a small
  admin SPA (gated by Admin-Key) for tenant lifecycle + eval dashboards.
- [ ] **Webhook / streaming push for ingestion**: clients poll job status. Next: add
  ingestion webhooks (tenant-provided callback URL, SSRF-guarded) or SSE job progress.

## Security (lower-priority, post-v9-SEC)

- [DONE] NAT64 SSRF, port restriction, log sanitization, admin-gated /metrics/docs/openapi,
  documented allowed_groups default, retrieved-chunk injection filter, admin-key fail-closed.
- [ ] **Tenant API key rotation UX**: currently operator-driven. Next: self-service rotation
  endpoint + key expiry.
- [DONE] **Self-service API key expiry (v10.8)**: complement to P1 #9 key tiers. Keys
  (`rk_*` secret + `pk_*` publishable) can now carry an optional UTC ISO-8601 `expires_at`;
  expired keys are rejected at resolution time (treated like revoked -> 401), so time-boxed
  keys + rotation-without-manual-revocation are possible. Endpoints: `POST /{tenant}/keys`
  (rotate, optional expires_at), `POST /{tenant}/keys/secret` (mint additional secret key,
  optional expires_at), `POST /{tenant}/keys/publishable` (optional expires_at),
  `PATCH /{tenant}/keys/{prefix}/expiry` (set/clear expiry; 404 if key gone/revoked).
  Malformed/naive expiry strings are rejected with 422. `TenantKey.expires_at` column added
  (nullable); `list_key_prefixes` surfaces it. PROD MIGRATION: `ALTER TABLE tenant_keys
  ADD COLUMN expires_at TIMESTAMP WITH TIME ZONE;` (idempotent — nullable). Covered by
  tests/test_security_fixes.py (test_*_expiry_*).
- [DONE] **Publishable / secret key tiers (P1 #9)**: tenants can now mint a read-only
  **publishable key** (`pk_*`) via `POST /api/v1/{tenant}/keys/publishable` that is safe to
  embed client-side (e.g. the widget). A publishable key resolves to the SAME tenant
  (isolation path untouched) but is scope-locked to query endpoints; `require_secret_key()`
  rejects it (403) from ingest/delete/rotate/revoke/admin/eval. The secret key (`rk_*`)
  keeps full power. Key tier is stored on `tenant_keys.kind` (default "secret").
  PROD MIGRATION: existing `tenant_keys` tables need `ALTER TABLE tenant_keys ADD COLUMN
  kind VARCHAR(16) NOT NULL DEFAULT 'secret';` (idempotent — `kind` column added with
  server_default). Covered by tests/test_security_fixes.py (test_publishable_key_*).
- [x] **Audit log**: tamper-evident, SHA-256 hash-chained `audit_log` table (app/audit.py),
  admin-gated `GET /audit` (read) + `GET /audit/verify` (chain-integrity proof). Records
  tenant.create / tenant.delete / key.rotate / key.revoke with a server-derived admin
  fingerprint (raw Admin-Key is never stored). Fail-open: audit write errors never block
  the primary operation. Covered by tests/test_security_fixes.py
  (test_audit_log_records_admin_actions, test_audit_chain_detects_tampering). NOTE: the
  audit trail deliberately does NOT touch the tenant-key-derived isolation path
  (auth.py / vector_store.py tenant_id filters) — that mechanism is out of scope by design.
- [ ] **mTLS / private network**: production should not expose /metrics/docs publicly even
  behind Admin-Key if on a public IP. Next: bind admin endpoints to an internal interface or
  require mTLS.

## Process

- [ ] Keep DESIGN-TRADEOFFS.md updated as each decision is made (multitenancy model,
  reranker default, encryption keyring, eval judge). Currently partially captured in commits.
