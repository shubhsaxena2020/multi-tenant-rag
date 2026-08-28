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
  non-breaking rotation. Next: schedule nightly snapshots via cron + offsite copy; run the
  DR drill in CI against a real Qdrant container (currently skipped under :memory:).
- [ ] **Horizontal scaling of ingestion workers**: the in-memory job queue (app/db.py jobs)
  is durable but single-replica. For N replicas, front with an at-least-once queue
  (Cloud Tasks / RQ / Celery) that calls the existing runner. Documented in requeue_orphaned_jobs().
  Next: add a `JOB_QUEUE` backend setting + a Cloud Tasks enqueue wrapper.
- [ ] **Per-tenant rate limiting / quotas**: rate_limit() exists but is global-ish; add
  per-tenant quotas (requests/min, chunks ingested, collection size) to prevent noisy
  neighbors. Next: store quota counters in the tenant registry; return 429 with Retry-After.
- [ ] **Connection pooling / worker tuning**: verify uvicorn workers × Qdrant pool sizing
  under load. Next: load-test with k6/locust; tune `workers` and `qdrant_timeout`.
- [ ] **Structured metrics cardinality audit**: MetricsMiddleware normalizes paths (good),
  but confirm no unbounded label still leaks (e.g. tenant_id in any metric). Next: grep all
  `.labels(` calls for tenant_id/raw-id usage.

## Product / Client-readiness

- [DONE] **Embeddable client widget + SDK + SSE**: app/static/widget.{js,html}, sdk.py,
  POST /api/v1/{tenant}/query/stream. Next: add a hosted demo + npm package for the SDK;
  add CORS policy config for the stream endpoint.
- [DONE] **Continuous eval & answer quality**: self-hosted LLM-as-judge (faithfulness +
  answer_relevancy), trend history, golden auto-gen. Next: build a Grafana panel over
  eval_runs; add scheduled nightly eval; add more metrics (context_precision, answer_correctness).
- [DONE] **SLOs/alerting**: /health/slo + deploy/alert.rules.yml. Next: ship a Prometheus
  scrape config + Grafana dashboard JSON in deploy/.
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
- [ ] **Audit log**: structured logs exist but no tamper-evident audit trail of admin actions.
  Next: append-only audit table + hash-chain.
- [ ] **mTLS / private network**: production should not expose /metrics/docs publicly even
  behind Admin-Key if on a public IP. Next: bind admin endpoints to an internal interface or
  require mTLS.

## Process

- [ ] Keep DESIGN-TRADEOFFS.md updated as each decision is made (multitenancy model,
  reranker default, encryption keyring, eval judge). Currently partially captured in commits.
