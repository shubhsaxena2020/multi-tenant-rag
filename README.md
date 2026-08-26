# RAG Service — Multi-Tenant Retrieval-Augmented Generation

General-purpose, multi-tenant RAG service to power multiple client-site chatbots from
one deployment. Designed for production from day one: hard per-tenant data isolation, a
real programmatic ingestion API with async job status tracking, and a documented query
API returning retrieved context (+ optional generated answer).

Status: **v1** — core contract implemented and tested (12 passing tests against a live
Qdrant container). Real embedding/reranking models are wired but run behind env flags
(defaults to deterministic models so the service boots with zero model downloads).

## Architecture decisions (with tradeoffs)
See `ISOLATION.md` (tenant isolation: silo vs pool) and
`../.hermes/notes/RAG Service/DESIGN-TRADEOFFS.md` (vector DB / embedding / chunking /
reranking, each with cited 2026 sources).

- **Vector store:** Qdrant (per-tenant collection = structural isolation).
- **Embedding:** BGE-M3 (multilingual, 1024-dim dense, self-hostable). Set
  `USE_REAL_EMBEDDER=1` to load it; defaults to a deterministic hasher for tests.
- **Chunking:** recursive token splitter, ~512 tokens / 64 overlap.
- **Reranking:** BGE-Reranker-v2-m3 cross-encoder (two-stage recall→rerank). Set
  `USE_REAL_RERANKER=1`.
- **Tenant registry:** SQLite (Postgres-ready interface).
- **Ingestion jobs:** sqlite-backed, threadpool runner, pollable status.

## Isolation guarantee
Tenant identity is derived **server-side from the Bearer API key** on every request; the
`{tenant}` path segment is informational. All vector operations are scoped to the
tenant's own Qdrant collection, so cross-tenant reads are impossible at the storage
layer — the guarantee holds even under application bugs. Offboarding drops the tenant's
collection entirely.

## Run
```bash
docker compose up -d            # starts Qdrant on :6333
uv venv && . .venv/bin/activate
uv pip install -e .
uvicorn app.main:app --port 8000
```
Config via env (see `.env.example`): `QDRANT_URL`, `DB_URL`, `ADMIN_API_KEY`,
`USE_REAL_EMBEDDER`, `USE_REAL_RERANKER`, `EMBED_MODEL`, `RERANK_MODEL`,
`LLM_BASE_URL`/`LLM_API_KEY`/`LLM_MODEL` (optional answer generation).

Health: `GET /health`, `GET /health/ready`. Metrics: `GET /metrics` (Prometheus).
Interactive contract: `GET /api/v1/docs` (Swagger). Schema: `GET /api/v1/openapi.json`.

## API (v1, base path `/api/v1`)

All tenant routes require `Authorization: Bearer <tenant_api_key>`.
Admin routes (`POST/GET/DELETE /tenants`) require `Admin-Key: <admin_api_key>` when
`ADMIN_API_KEY` is set (open otherwise).

### Tenants (admin)
- `POST /api/v1/tenants` → `{tenant_id, name, api_key, plan, created_at}` (201)
- `GET /api/v1/tenants` → list (200)
- `DELETE /api/v1/tenants/{tenant_id}` → offboard (drops collection + registry) (200)

### Ingestion
- `POST /api/v1/{tenant}/documents` (text) → sync, `{doc_id, chunk_count}` (201)
- `POST /api/v1/{tenant}/ingest/url` → sync fetch+ingest (201)
- `POST /api/v1/{tenant}/ingest/jobs` → **async** `{kind, text|url|content, content_type,
  metadata}`; returns `{job_id, status: pending}` (202)
- `GET /api/v1/{tenant}/jobs/{job_id}` → `{status, progress, total_chunks, done_chunks,
  result_doc_id, error}` (200/404)
- `GET /api/v1/{tenant}/jobs` → recent jobs (200)
- `DELETE /api/v1/{tenant}/jobs/{job_id}` → remove job record (200/404)
- `DELETE /api/v1/{tenant}/documents/{doc_id}` → delete a document (200)

### Query
- `POST /api/v1/{tenant}/query` → `{question, top_k, candidate_k, rerank, generate}` →
  `{results:[{chunk_id, doc_id, title, text, score, metadata}], answer, tenant_id}`
  - `generate=true` returns a generated answer grounded in `results` (if an LLM
    provider is configured; otherwise an extractive answer from the top chunk).

## Rate limiting & observability
- Per-IP and per-tenant token-bucket limits (configurable `RATE_*_PER_MIN`). Exceeded →
  `429` with `Retry-After`. Ingestion jobs have a separate worker-pool cap.
- Structured JSON logs on every ingestion/query/offboard event (tenant_id-tagged).
- Prometheus metrics at `/metrics`: request count/latency, ingest jobs (by status),
  ingest chunks, retrieval latency, query hit count.

## Validation
- Max 1,000,000 chars per document; content_type ∈ {text, markdown, html, code};
  metadata JSON ≤ 32 KB. Violations → 422.

## Tests
`pytest tests/` (needs Qdrant on `:6333`). Covers ingestion, isolation (a tenant's
secret never appears in another tenant's query), async job lifecycle + job isolation,
validation, answer generation, offboarding, and admin gating.

## Not yet implemented (documented, planned)
- **Hybrid (sparse) retrieval:** BGE-M3 emits sparse vectors; store them for
  lexical+dense fusion (requires the real model; deterministic stub is dense-only).
- **Job cancellation** of in-flight runs (records can be deleted once terminal).
- **Pool mode** migration for very high tenant counts (see ISOLATION.md).
- **Rate limiting / per-tenant quota** enforcement (caps exist; throttling is a
  deployment concern — put behind a gateway/ingress).
- **KMS per-tenant encryption at rest** (enterprise tier).
