# RAG Service — Multi-Tenant Retrieval-Augmented Generation

General-purpose, multi-tenant RAG platform to power multiple independent client-site
chatbots from one self-hosted deployment. Built for production from day one: hard
per-tenant data isolation, a real programmatic ingestion API with async job tracking, a
documented versioned query API, hybrid (dense+sparse) retrieval with cross-encoder
reranking, encryption-at-rest, document-level RBAC, per-tenant quotas, key rotation, an
offline eval harness, rate limiting and Prometheus observability.

Status: **v3** — industrial feature set implemented and verified (20 passing tests
against a live Qdrant container + live runtime smoke). Real embedding/reranking models
are wired but run behind env flags (defaults to deterministic models so the service
boots with zero model downloads).

## Architecture decisions (with tradeoffs + citations)
See `ISOLATION.md` (tenant isolation: silo vs pool) and
`../.hermes/notes/RAG Service/DESIGN-TRADEOFFS.md` + `RESEARCH-2026.md` (vector DB /
embedding / chunking / reranking / isolation / architecture, each with dated 2026
sources).

- **Vector store:** Qdrant. Each tenant gets its own collection (structural
  isolation). Capable of 100M+ vectors, distributed, native hybrid (dense + sparse
  vectors). Benchmarked 4 ms p50 / 25 ms p99 (salttechno 2026).
- **Embedding:** BGE-M3 (multilingual, 1024-dim dense + sparse in one pass, MIT,
  CPU-friendly). Set `USE_REAL_EMBEDDER=1` to load it; defaults to a deterministic
  embedder for tests. Optional TEI URL (`EMBED_BASE_URL`) offloads to a GPU node.
- **Chunking:** recursive token splitter, ~512 tokens / 64 overlap (validated 2026
  benchmark band: 69% accuracy vs 54% semantic).
- **Retrieval:** hybrid dense + sparse (BGE-M3) → Reciprocal Rank Fusion (RRF) →
  cross-encoder rerank. RRF hybrid lift ≈ +8–14 recall@10 (Agile Infoways 2026).
- **Reranking:** BGE-Reranker-v2-m3 cross-encoder (two-stage recall→rerank, default ON).
  Set `USE_REAL_RERANKER=1`.
- **Encryption-at-rest:** AES-GCM envelope, per-tenant key derived from a master key
  (KMS-style). Set `MASTER_ENCRYPTION_KEY`; chunk text is sealed in Qdrant, never
  plaintext.
- **Document-level RBAC:** optional per-tenant sub-user groups. Chunks carry an `acl`;
  queries pass a group filter applied at the Qdrant layer (cannot be bypassed).
- **Tenant registry:** SQLite (Postgres-ready interface); multiple rotatable hashed API
  keys per tenant; per-tenant chunk quota for fleet protection.
- **Ingestion jobs:** sqlite-backed, threadpool runner, pollable status.

## Isolation guarantee
Tenant identity is derived **server-side from the Bearer API key** on every request; the
`{tenant}` path segment is informational. All vector operations are scoped to the
tenant's own Qdrant collection, so cross-tenant reads are impossible at the storage
layer — the guarantee holds even under application bugs. Offboarding drops the tenant's
collection entirely. Document-level RBAC is layered *on top* of this silo, applied at
the database filter layer.

## Run
```bash
docker compose up -d            # starts Qdrant (+ optional Redis) on :6333
uv venv && . .venv/bin/activate
uv pip install -e .
uvicorn app.main:app --port 8000
```
# Admin key required for admin routes. Set `ADMIN_API_KEY=***` in `.env` before first start.
# The `Admin-Key` header (e.g. `Admin-Key: admin_master_key`) is needed for tenant create,
# key rotation, and other admin operations. Without it, those routes return 403.
# Ensure `docker compose.yml` includes `env_file: - .env` so .env values load into the app container.
Config via env (see `.env.example`): `QDRANT_URL`, `DB_URL`, `ADMIN_API_KEY`,
`MASTER_ENCRYPTION_KEY`, `USE_REAL_EMBEDDER`, `USE_REAL_RERANKER`, `EMBED_MODEL`,
`EMBED_BASE_URL` (TEI), `RERANK_MODEL`, `TENANT_CHUNK_QUOTA`,
`RATE_*_PER_MIN`, `LLM_BASE_URL`/`LLM_API_KEY`/`LLM_MODEL` (optional answer generation).

# Admin key setup
# Set `ADMIN_API_KEY=***` in `.env` before first start.
# The `Admin-Key` header (e.g. `Admin-Key: admin_master_key`) is required for
# tenant create, key rotation, and all other admin routes. Without it, those
# routes return 403 "Admin key required". Admin audit endpoints (`/audit`,
# `/audit/verify`) also fail-closed without the Admin-Key.

Health: `GET /health`, `GET /health/ready`. Metrics: `GET /metrics` (Prometheus).
Interactive contract: `GET /api/v1/docs` (Swagger). Schema: `GET /api/v1/openapi.json`.
Admin audit trail: `GET /audit` (read, hash-chained) + `GET /audit/verify` (chain-integrity
proof) — both gated behind `Admin-Key`, fail-closed.

## API (v1, base path `/api/v1`)

All tenant routes require `Authorization: Bearer <tenant_api_key>`.
Admin routes (`POST/GET/DELETE /tenants`) require `Admin-Key: <admin_api_key>` when
`ADMIN_API_KEY` is set (open otherwise).

### Tenants (admin)
- `POST /api/v1/tenants` → `{tenant_id, name, api_key, plan, created_at, chunk_count}` (201)
- `GET /api/v1/tenants` → list (200)
- `DELETE /api/v1/tenants/{tenant_id}` → offboard (drops collection + registry) (200)

### API keys (tenant-scoped)
- `POST /api/v1/{tenant}/keys` → issue a new key (old key stays valid) (201)
- `GET /api/v1/{tenant}/keys` → list key prefixes + revoked state (200)
- `DELETE /api/v1/{tenant}/keys/{prefix}` → revoke a key (last valid key protected) (200)

### Ingestion
- `POST /api/v1/{tenant}/documents` (text) → sync, `{doc_id, chunk_count}` (201)
- `POST /api/v1/{tenant}/ingest/url` → sync fetch+ingest (201)
- `POST /api/v1/{tenant}/ingest/jobs` → **async** `{kind, text|url|content, content_type,
  metadata, acl?}`; returns `{job_id, status: pending}` (202)
- `GET /api/v1/{tenant}/jobs/{job_id}` → `{status, progress, total_chunks, done_chunks,
  result_doc_id, error}` (200/404)
- `GET /api/v1/{tenant}/jobs` → recent jobs (200)
- `DELETE /api/v1/{tenant}/jobs/{job_id}` → remove job record (200/404)
- `DELETE /api/v1/{tenant}/documents/{doc_id}` → delete a document (200)
- Optional `acl: [group, ...]` on any ingest body mirrors source ACL into chunk metadata.

### Query
- `POST /api/v1/{tenant}/query` → `{question, top_k, candidate_k, rerank, generate, acl?}`
  → `{results:[{chunk_id, doc_id, title, text, score, metadata}], answer, tenant_id}`
  - `acl: [group, ...]` restricts retrieval to chunks whose `acl` intersects those
    groups (document-level RBAC at the DB layer).
  - `generate=true` returns a generated answer grounded in `results` (if an LLM
    provider is configured; otherwise an extractive answer from the top chunk).

### Evaluation (offline, no prod traffic)
- `PUT /api/v1/{tenant}/eval/set` → store a golden set `{items:[{question,
  relevant_doc_ids?, relevant_texts?, expected_answer?}]}` (200)
- `POST /api/v1/{tenant}/eval/run` → `{questions, hit_rate, mrr, ndcg, context_recall,
  avg_latency_ms}` (200)

## Rate limiting, quotas & observability
- Per-IP and per-tenant token-bucket limits (configurable `RATE_*_PER_MIN`). Exceeded →
  `429` with `Retry-After`. Ingestion jobs have a separate worker-pool cap.
- Per-tenant **chunk quota** (`TENANT_CHUNK_QUOTA`): ingestion past the cap → `429`
  with quota headers. Fleet protection so one tenant can't starve the cluster.
- Structured JSON logs on every ingestion/query/offboard/key event (tenant_id-tagged).
- Prometheus metrics at `/metrics`: request count/latency, ingest jobs (by status),
  ingest chunks, retrieval latency, query hit count — all tenant-labelled.

## Validation
- Max 1,000,000 chars per document; content_type ∈ {text, markdown, html, code};
  metadata JSON ≤ 32 KB. Violations → 422.

## Tests
`pytest tests/` (needs Qdrant on `:6333`). Covers: hybrid retrieval wiring, hard tenant
isolation (a tenant's secret never appears in another tenant's query), encryption-at-
rest (raw Qdrant payload is ciphertext), document-level RBAC, async job lifecycle +
job isolation, API-key rotation + revocation, tenant chunk quota, offline eval,
validation, answer generation, offboarding, admin gating, rate limiting, metrics, and
the versioned OpenAPI contract. Deterministic models in tests (`USE_REAL_EMBEDDER=0
USE_REAL_RERANKER=0`) so the full hybrid pipeline is verified with zero downloads.

## Scaling path
Qdrant cluster (sharding + replicas) + stateless app replicas behind a load balancer +
Redis-backed rate limiter (multi-instance) + TEI GPU node for embedding + K8s/HPA. See
`DEPLOYMENT.md`.
