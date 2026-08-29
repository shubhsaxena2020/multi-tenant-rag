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
- [DONE] **Startupschema migration guard (Fixes #2)**: `init_db()` now idempotently adds
  missing additive columns (`tenant_keys.kind`, `tenant_keys.expires_at`, `tenants.chunk_quota`)
  to pre-existing tables via dialect-aware introspection (SQLite PRAGMA / Postgres
  information_schema), because `create_all` only creates missing tables, not new columns.
  Prevents the "table X has no column named Y" failure class on existing prod DBs. Real GitHub
  issue #2 filed + auto-closed by this PR. Covered by
  tests/test_security_fixes.py::test_init_db_adds_missing_columns_to_existing_tables.
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
<!-- APPENDED NEXT-CHAPTER BACKLOG — generated 2026-08-30 -->

# Next-Chapter Backlog (agent-1 autonomous work program)

This section extends the research backlog above with an EXHAUSTIVE, sequenced work program
so the agent always has a concrete next item. Work strictly via
branch → PR → self-merge(squash) → tag → release. After each phase completes, self-select
the next phase. Each task has a one-line "done when" criterion.

> Decisions carried from the orchestrator:
> - **Issue #22 (safe Markdown rendering in the embeddable widget)** — agent-1 TAKES it. It is
>   an early item below (Phase A). Not a human gate.
> - **Issue #15 (fail-closed on path-tenant vs key-tenant mismatch)** — recorded as a KNOWN
>   LIMITATION / orchestrator-owned hardening item (Phase H). Do NOT silently expand into
>   tenancy/key-tier code (P0/P1 territory the orchestrator owns); document the gap instead.
> - Short goals cause idle drift — work the phases in order, never stop mid-program without
>   committing a completed phase.

---

## Phase A — Widget hardening (#22)  [START HERE]
1. [ ] Audit `app/static/widget.html` + `widget.js` for any `innerHTML`/unsafe insertion of
   answer or source text. **done when** a grep for `innerHTML`/`insertAdjacentHTML` on
   untrusted strings returns no unsafe hits.
2. [ ] Add a minimal, allow-list Markdown renderer (e.g. vendored `marked` + `DOMPurify`, or a
   small safe subset) to `widget.html`. **done when** a `renderMarkdown()` helper exists and is
   unit-testable in isolation.
3. [ ] Render retrieved `answer` and `text` (source chunks) through `renderMarkdown()` instead
   of raw text. **done when** the demo widget shows bold/lists/links in answers without XSS.
4. [ ] Sanitize all citation/source metadata before display (escape `title`/`doc_id`).
   **done when** injecting a source `title` containing `<img onerror>` renders inert.
5. [ ] Add a regression test page or Playwright check asserting malicious Markdown in an answer
   cannot execute script in the widget iframe. **done when** the test fails on an unsafe
   renderer and passes on the safe one.
6. [ ] Commit Phase A on `feat/widget-safe-markdown`, open PR, self-merge, tag
   `v16.48-widget-safe-md`, update NEEDS_HUMAN to close #22. **done when** tag exists and #22
   marked closed in pane.

## Phase B — Query rewriting & decomposition
7. [ ] Add `app/retrieval/rewrite.py` with a `rewrite_query(question, history)` entrypoint that
   is a passthrough when `LLM_BASE_URL` is unset. **done when** calling it with no LLM returns
   the question unchanged.
8. [ ] Implement LLM-backed rewrite: short-query expansion + multi-part decomposition into
   sub-questions (gated behind `LLM_BASE_URL`/`LLM_MODEL`). **done when** a 4-word query
   produces a single clarified rewrite; a "compare X and Y" query produces 2 sub-questions.
9. [ ] Wire `rewrite` into `POST /{tenant}/query` (default ON, `rewrite=false` to skip).
   **done when** query response includes `rewritten_query` field when enabled.
10. [ ] Log rewrite usage + latency to observability. **done when** metrics show rewrite counts.
11. [ ] Add tests for passthrough + LLM rewrite using the deterministic embedder and a mocked
    LLM. **done when** `pytest tests/test_rewrite.py` is green.
12. [ ] Commit Phase B, PR, merge, tag `v16.49-query-rewrite`.

## Phase C — Agentic / multi-hop retrieval
13. [ ] Add `app/retrieval/agentic.py` with a `retrieve_multi_hop(question, max_hops=3)` loop:
    retrieve → read top chunks → reformulate follow-up → re-retrieve. **done when** a 2-hop
    question returns chunks from both hops.
14. [ ] Gate multi-hop behind a tenant `plan`/feature flag (`allow_multi_hop`). **done when**
    free-tier tenants get 1 hop; paid get up to N.
15. [ ] Expose `hops` param on `/query` and include `hop_count` in response. **done when**
    response carries the actual hops executed.
16. [ ] Cap token/latency cost (max hops, max chunks per hop) and add a circuit-breaker.
    **done when** a pathological query terminates within the cap.
17. [ ] Tests for single-hop equivalence + multi-hop improvement on a fixture corpus.
    **done when** green; multi-hop hit_rate >= single-hop on the fixture.
18. [ ] Commit, PR, merge, tag `v16.50-agentic-retrieval`.

## Phase D — Citation faithfulness & no-answer detection
19. [ ] Add `app/generation/faithfulness.py`: token-overlap + self-check prompt scoring of
    answer-vs-context. **done when** a grounded answer scores high; an ungrounded one low.
20. [ ] Emit `faithfulness` (0..1) + `answerable` flag in `/query` response. **done when**
    response schema includes both.
21. [ ] Log faithfulness distribution per tenant. **done when** metrics exist.
22. [ ] Add no-answer path: if `answerable=False`, return a safe "I don't know" with citations
    only. **done when** unanswerable query returns empty answer + 200.
23. [ ] Tests covering faithful/unfaithful/ungrounded cases. **done when** `tests/test_faithfulness.py`
    green.
24. [ ] Commit, PR, merge, tag `v16.51-faithfulness`.

## Phase E — Retrieval-quality observability
25. [ ] Persist per-query eval signals (latency, hit_count, faithfulness, rerank delta,
    rewrite used) to a small SQLite/metrics store. **done when** a query writes a row.
26. [ ] Extend `/metrics` with retrieval-quality gauges (faithfulness avg, no-answer rate).
    **done when** Prometheus scrape shows the new series.
27. [ ] Build a Grafana panel JSON (`deploy/grafana/dashboards/rag-quality.json`) over the new
    metrics. **done when** panel JSON renders the quality dashboard.
28. [ ] Add nightly eval hook reusing `PUT/POST /eval/set|run` to trend quality. **done when**
    a cron-able script runs eval and appends to the store.
29. [ ] Commit, PR, merge, tag `v16.52-retrieval-observability`.

## Phase F — Document parsing depth (LlamaParse-class, local-only)
30. [ ] Extend `app/ingestion/html_util.py` + `chunker.py` to preserve heading hierarchy and
    table boundaries as chunk metadata. **done when** a table ingested yields chunk metadata
    marking table rows.
31. [ ] Add confidence + citation spans to extracted chunks where derivable. **done when**
    chunk metadata includes a `char_span` for the source.
32. [ ] Support more `content_type` inputs (improved markdown/html/code segmentation).
    **done when** code blocks are not split mid-token.
33. [ ] Tests for table/heading/code ingestion fidelity. **done when** green.
34. [ ] Commit, PR, merge, tag `v16.53-parsing-depth`.

## Phase G — Self-serve onboarding + plan-gated ceilings
35. [ ] Turn `plan` into real feature gates: map plan → {max_top_k, allow_multi_hop,
    allow_rewrite, retention_days}. **done when** a free tenant hitting a paid-only feature
    gets a 402/403 with a clear message.
36. [ ] Build a minimal admin SPA (`app/static/admin.html` exists — extend it) over the
    existing admin API for tenant lifecycle + eval dashboards. **done when** an operator can
    create a tenant + view eval from the page.
37. [ ] Expose read-only metering (token usage / chunk count) in the admin SPA. **done when**
    the page shows per-tenant usage.
38. [ ] Tests for plan-gate enforcement. **done when** green.
39. [ ] Commit, PR, merge, tag `v16.54-plan-gates`.

## Phase H — Known limitations & hardening (document, don't over-reach)
40. [ ] Document **issue #15 (path-tenant vs key-tenant mismatch)** as a known limitation in
    ISOLATION.md: the `/{tenant}/documents` route is key-scoped (returns the key's own data)
    and does NOT fail-closed on a path-tenant≠key-tenant mismatch (returns 200 with own data);
    the jobs route already 404s. **done when** ISOLATION.md has a "Known limitation" subsection.
41. [ ] Add a test asserting current behavior (key-scoped, 200) so the gap is tracked, and
    leave a TODO noting this is orchestrator-owned P0/P1 tenancy hardening. **done when** test
    documents current behavior without changing it.
42. [ ] Audit `rate_limit()` and record the per-tenant quota gap as a tracked item (see Phase I).
    **done when** a NEEDS_HUMAN note lists it.

## Phase I — Per-tenant rate limiting / quotas (from prior backlog)
43. [ ] Store per-tenant quota counters (req/min, chunks ingested, collection size) in the
    tenant registry. **done when** counters increment on each call.
44. [ ] Enforce per-tenant 429 with `Retry-After` distinct from the global limiter.
    **done when** one tenant over-quota gets 429 while others are unaffected.
45. [ ] Tests for tenant isolation of limits. **done when** green.
46. [ ] Commit, PR, merge, tag `v16.55-tenant-quotas`.

## Phase J — Ingestion webhooks + horizontal workers (from prior backlog)
47. [ ] Add SSRF-guarded tenant callback URL on ingest job completion. **done when** a safe
    localhost/allowlisted URL receives a job-done POST.
48. [ ] Add `JOB_QUEUE` backend setting (RQ/Celery-style wrapper around existing runner) for
    multi-replica safety. **done when** a queue backend can enqueue/run a job.
49. [ ] Document requeue_orphaned_jobs() runbook for multi-replica. **done when** runbook
    exists in DEPLOYMENT.md.
50. [ ] Commit, PR, merge, tag `v16.56-ingestion-scale`.

## Phase K — Continuous improvement loop
51. [ ] After each release, re-run the bounded test suite + nightly eval and append a one-line
    result to a CHANGELOG-style note. **done when** each tag has a recorded result.
52. [ ] Keep DESIGN-TRADEOFFS.md updated as each decision is made. **done when** new decisions
    are captured within the PR that introduces them.
