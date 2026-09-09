# RAG Service — a self-hostable multi-tenant RAG template

Clone this repo, run one command, and you have a production-shaped **multi-tenant
Retrieval-Augmented Generation** service for your own project. Everything runs from this
repo — the only thing you install is Docker.

```bash
git clone <this-repo> rag-service && cd rag-service && ./setup.sh
```

`setup.sh` generates secrets, starts the stack, waits for health, and creates your first
tenant. Full walkthrough — for a human or an AI agent — in **[SETUP.md](SETUP.md)**.

> **What this is:** a template you stand up per project. Many isolated tenants
> (API namespaces), per-tenant keys / quotas / Qdrant collections, document-level RBAC,
> encryption-at-rest, hybrid retrieval + rerank, async ingestion, an eval harness, and a
> Prometheus/Grafana stack.
>
> **What it isn't:** a hosted SaaS you operate for external customers. No signup portal,
> no billing, no per-user password store. "Tenants" are namespaces you create with your
> own admin key.

The service boots with **deterministic placeholder models** by default — the full hybrid
retrieval + rerank pipeline runs with zero model downloads, so it starts in seconds and
the test suite passes offline. Flip `USE_REAL_EMBEDDER=1` / `USE_REAL_RERANKER=1` when
you're ready for real weights.

---

## Architecture decisions (with tradeoffs + citations)

See `ISOLATION.md` (tenant isolation: silo vs pool) and `DESIGN-TRADEOFFS.md` (vector DB /
embedding / chunking / reranking / isolation / architecture, each with dated 2026 sources).

- **Vector store:** Qdrant. Each tenant gets its own collection (structural isolation).
  100M+ vectors, distributed, native hybrid (dense + sparse) vectors.
- **Embedding:** dense + sparse in one pass (default `intfloat/multilingual-e5-large`
  1024-dim + SPLADE sparse; set `EMBED_MODEL=BAAI/bge-m3` +
  `EMBED_PROVIDER=sentence_transformers` for full BGE-M3). `USE_REAL_EMBEDDER=1` loads
  real weights; a deterministic embedder is the default. `EMBED_BASE_URL` offloads
  embedding to a TEI GPU node.
- **Chunking:** recursive token splitter, ~512 tokens / 64 overlap.
- **Retrieval:** hybrid dense + sparse → Reciprocal Rank Fusion (RRF) → cross-encoder
  rerank. RRF hybrid lift ≈ +8–14 recall@10 (Agile Infoways 2026).
- **Reranking:** BGE-Reranker-v2-m3 cross-encoder (two-stage recall→rerank, default ON;
  `USE_REAL_RERANKER=1`).
- **Encryption-at-rest:** AES-GCM envelope, per-tenant key derived from
  `MASTER_ENCRYPTION_KEY` (KMS-style). Chunk text is sealed in Qdrant, never plaintext.
- **Document-level RBAC:** new tenants default to `allowed_groups=["__public__"]`, so a
  query that omits `acl` only sees public-tagged chunks. Operators can provision named
  groups and callers must pass an allowed group; explicitly setting `allowed_groups=["*"]`
  preserves unrestricted wildcard behavior for existing/single-user tenants.
- **Tenant registry:** async SQLAlchemy — SQLite for dev/CI, Postgres-ready interface.
  Multiple rotatable hashed keys per tenant; publishable (`pk_`) vs secret (`rk_`) tiers;
  optional key expiry; per-tenant chunk quota.
- **Ingestion jobs:** DB-backed store + threadpool runner + pollable status; optional
  Redis job-distribution backend for horizontal workers (`JOB_QUEUE_BACKEND=redis`).
- **Outbound webhooks:** optional per-tenant HMAC-signed callbacks for lead capture and
  job completion (SSRF-guarded egress).

## Isolation guarantee

Tenant identity is derived **server-side from the Bearer API key** on every request; the
`{tenant}` path segment is informational. All vector operations are scoped to the
tenant's own Qdrant collection, so cross-tenant reads are impossible at the storage
layer — the guarantee holds even under application bugs. Offboarding drops the tenant's
collection entirely. Document-level RBAC is layered *on top* of this silo, applied at the
database filter layer.

## Quick start (details in SETUP.md)

```bash
./setup.sh                       # docker + .env + compose up + first tenant
# or by hand:
cp .env.example .env             # then set ADMIN_API_KEY + MASTER_ENCRYPTION_KEY
docker compose up -d             # Qdrant + app on :6333 / :8000
curl -s localhost:8000/health    # {"status":"ok"}
```

Key config (full list in `.env.example`, definitions in `app/config.py`):
`ADMIN_API_KEY` (gates tenant admin routes), `MASTER_ENCRYPTION_KEY` (encryption-at-rest),
`USE_REAL_EMBEDDER` / `USE_REAL_RERANKER`, `EMBED_MODEL` / `EMBED_BASE_URL`, `RERANK_MODEL`,
`TENANT_CHUNK_QUOTA`, `RATE_*_PER_MIN`, `REDIS_URL`, `LLM_BASE_URL` / `LLM_API_KEY` /
`LLM_MODEL` (optional answer generation).

Surfaces: `/api/v1/docs` (Swagger), `/demo`, `/admin/console`, `/metrics`, `/widget.js`.

## API (v1, base path `/api/v1`)

Tenant routes require `Authorization: Bearer <tenant_api_key>`. Admin routes
(`POST/GET/DELETE /tenants`, `/admin/*`, `/audit*`) require `Admin-Key: <admin_api_key>`
when `ADMIN_API_KEY` is set (open otherwise) and **fail closed** without it.

### Tenants (admin)
- `POST /api/v1/tenants` → `{tenant_id, name, api_key, plan, created_at, chunk_count}` (201)
- `GET /api/v1/tenants` → list (200)
- `DELETE /api/v1/tenants/{tenant_id}` → offboard (drops collection + registry) (200)

### API keys (tenant-scoped)
- `POST /api/v1/{tenant}/keys` / `.../keys/secret` → issue a secret `rk_` key (old stays valid)
- `POST /api/v1/{tenant}/keys/publishable` → issue a read-only `pk_` key for the widget
- `GET /api/v1/{tenant}/keys` → list key prefixes + revoked state
- `DELETE /api/v1/{tenant}/keys/{prefix}` → revoke (last valid key protected)
- `PATCH /api/v1/{tenant}/keys/{prefix}/expiry` → time-box a key

### Ingestion
- `POST /api/v1/{tenant}/ingest/text` — sync, `{doc_id, chunk_count}` (201)
- `POST /api/v1/{tenant}/ingest/url` — SSRF-checked fetch + ingest (201)
- `POST /api/v1/{tenant}/documents/upload` — multipart file (pdf/docx/md/txt/code)
- `POST /api/v1/{tenant}/ingest/jobs` — **async** `{job_id, status: pending}` (202)
- `GET /api/v1/{tenant}/jobs` / `.../jobs/{job_id}` — list / poll `{status, progress, …}`
- `DELETE /api/v1/{tenant}/jobs/{job_id}` — remove job record
- `POST /api/v1/{tenant}/ingest/sitemap` — crawl a sitemap.xml (async)
- `GET /api/v1/{tenant}/documents` — paginated catalog · `GET/DELETE .../documents/{doc_id}`
- Optional `acl: [group, …]` on any ingest body mirrors source ACL into chunk metadata.

### Query
- `POST /api/v1/{tenant}/query` → `{question, top_k, candidate_k, rerank, generate, acl?}`
  → `{results:[{chunk_id, doc_id, title, text, score, metadata}], answer, tenant_id,
  faithfulness, answerable, out_of_scope}`
  - `acl: [group, …]` restricts retrieval to chunks whose `acl` intersects those groups.
  - `generate=true` returns an LLM answer grounded in `results` (if `LLM_*` set), else an
    extractive answer from the top chunk.
- `POST /api/v1/{tenant}/query/stream` — SSE streaming variant.

### Evaluation (offline, no prod traffic)
- `PUT /api/v1/{tenant}/eval/set` → store a golden set
- `POST /api/v1/{tenant}/eval/run` → `{questions, hit_rate, mrr, ndcg, context_recall,
  avg_latency_ms}`

### Widget / branding / business
`GET /{tenant}/widget/config`, `GET /{tenant}/session/{session_id}` (multi-turn history),
`PATCH /{tenant}/branding`, `PATCH /{tenant}/system-prompt`, `PATCH /{tenant}/webhooks`,
`GET /api/v1/{tenant}/usage`, `POST /api/v1/{tenant}/feedback`, `POST /api/v1/{tenant}/handoff`.

## Rate limiting, quotas & observability

- Per-IP and per-tenant token-bucket limits (`RATE_*_PER_MIN`). Exceeded → `429` +
  `Retry-After`. Ingestion jobs have a separate cap. Set `REDIS_URL` (and start
  `--with-redis`) to share one budget across replicas.
- Per-tenant **chunk quota** (`TENANT_CHUNK_QUOTA`): ingestion past the cap → `429` with
  quota headers, so one tenant can't exhaust shared storage.
- Structured JSON logs on every ingest / query / offboard / key event (tenant-tagged).
- Prometheus metrics at `/metrics`: request count/latency, ingest jobs by status, ingest
  chunks, retrieval latency, query hit count, no-answer rate, faithfulness histogram —
  all tenant-labelled. Grafana dashboards under `deploy/grafana/`.
- Tamper-evident audit log: `GET /audit` (hash-chained) + `GET /audit/verify`.

## Validation

Max 1,000,000 chars per document; `content_type ∈ {text, markdown, html, code}`;
metadata JSON ≤ 32 KB. Violations → `422`.

## Tests

```bash
docker compose exec app python -m pytest -q      # or: pip install -e ".[test]" && pytest -q
```
Deterministic models in tests (`USE_REAL_EMBEDDER=0 USE_REAL_RERANKER=0`), so the full
hybrid pipeline — chunk → embed → Qdrant dense+sparse → RRF → rerank → RBAC filter — is
verified with zero downloads and no external services for most of the suite. Covers hard
tenant isolation, encryption-at-rest (raw payload is ciphertext), RBAC, async job
lifecycle + isolation, key rotation/revocation/expiry, quotas, offline eval, SSRF guard,
webhooks, admin gating, rate limiting, metrics, and the versioned OpenAPI contract.

## Scaling path

Qdrant cluster (sharding + replicas) + stateless app replicas behind a load balancer +
`JOB_QUEUE_BACKEND=redis` for horizontal ingestion workers + Redis-backed rate limiter +
TEI GPU node for embedding + K8s/HPA. See `DEPLOYMENT.md`.

## Repository layout

`app/` — the service (`main.py` routes, `config.py` every setting, `vector_store.py`
Qdrant silo, `crypto.py` encryption, `rbac.py`, `ingestion/`, `retrieval/`, `webhook.py`,
`job_queue.py`). `tests/` — the suite. `deploy/` — Prometheus/Alertmanager/Grafana.
`docs/` — operator runbooks. `sdk.py` / `sdk-js/` — Python & TypeScript clients.
`setup.sh` / `SETUP.md` — the clone-and-go path.
