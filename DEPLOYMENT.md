# Deployment — RAG Service

## 1. Local / single-VPS self-host (now)

`docker-compose.yml` brings up everything needed on one machine:

- **Qdrant** (vector store, per-tenant collections) on `:6333`
- **rag-service** (FastAPI app) on `:8000`, built from the local `Dockerfile`
- **Redis** (optional, for shared rate-limit state when you run >1 app replica — see §2)

```bash
cp .env.example .env          # set ADMIN_API_KEY, optionally USE_REAL_EMBEDDER=1
docker compose build
docker compose up -d
curl http://localhost:8000/health
curl http://localhost:8000/metrics      # Prometheus metrics
```

Create a tenant (admin):
```bash
curl -X POST http://localhost:8000/api/v1/tenants \
  -H 'Admin-Key: <ADMIN_API_KEY>' -H 'Content-Type: application/json' \
  -d '{"name":"acme"}'
```
The response includes `api_key`. Use it as `Authorization: Bearer <api_key>`.

Ingest (async, status-tracked):
```bash
curl -X POST http://localhost:8000/api/v1/acme/ingest/jobs \
  -H 'Authorization: Bearer <api_key>' -H 'Content-Type: application/json' \
  -d '{"kind":"text","title":"faq","text":"...","content_type":"text"}'
# poll: GET /api/v1/acme/jobs/{job_id}
```

Query:
```bash
curl -X POST http://localhost:8000/api/v1/acme/query \
  -H 'Authorization: Bearer <api_key>' -H 'Content-Type: application/json' \
  -d '{"question":"...","top_k":8,"generate":false}'
```

The interactive contract lives at `http://localhost:8000/api/v1/docs` (Swagger).

### Resource notes
- Deterministic embedder/reranker by default → boots with **no model downloads**.
- For production quality set `USE_REAL_EMBEDDER=1` + `USE_REAL_RERANKER=1` → loads
  BGE-M3 (568M) and BGE-Reranker-v2-m3 (568M, multilingual, MIT). Budget ~2 GB RAM +
  a CPU is fine; a small GPU accelerates embedding throughput.
- Per-tenant Qdrant collection = one HNSW index in RAM. At hundreds of tenants, plan
  RAM = tenants × (vectors × dim × 4 bytes × ~1.3 HNSW overhead). Use binary/products
  quantization (Qdrant) to compress 32× if memory-bound.

## 2. Horizontal scaling (later)

The app is **stateless** except for (a) Qdrant and (b) rate-limit state. Scale each
independently:

### Qdrant (stateful — the only hard state)
- **Single node** already handles ~100M+ vectors (Q1 2026 VectorDBBench: billions in
  distributed mode; Kunal Ganglani 2026: 100M+ single node).
- **Cluster / sharding** when you exceed one node: run Qdrant in cluster mode
  (multiple nodes, shard replicas), point `QDRANT_URL` at the cluster entrypoint.
  Collections are sharded automatically; per-tenant isolation is preserved because
  each tenant still owns its own collection.
- Enable on-disk payload + vectors, and binary/product quantization to cut RAM.

### App replicas (stateless)
- Run N copies of the `rag-service` image behind a load balancer / ingress
  (nginx, Caddy, Traefik, or your cloud LB). They share nothing but Qdrant + Redis.
- **Rate-limit state**: set `REDIS_URL` to a shared Redis (or Redis Cluster) instance and

- **Orphan job recovery**: when a worker crashes or restarts while processing a job,
  the job may be left in a partial state. The `recover_orphans()` method from the
  queue backend handles recovery. Run it manually or via a scheduled cron job:
```bash
# Check for orphaned jobs (older than 5 minutes)
source .venv/bin/activate
python3 -c "
from app.queue import register_backend, get_backend, recover_orphans
register_backend('inline')
backend = get_backend()
orphans = backend.recover_orphans(older_than_seconds=300.0)
print(f'Recovered {len(orphans)} orphaned jobs')
for o in orphans:
    print(f'  - {o[\"job_id\"]}: {o[\"job_type\"]}")
```
For Redis/RQ/Celery backends, the recovery uses the queue service's native tools to
re-queue jobs that were `started` but not `completed`/`failed` — centralized in the
queue service, not per-replica.
  the per-IP/per-tenant token bucket is stored in Redis via an atomic Lua script, so all
  replicas enforce ONE global budget (a noisy tenant is capped across the whole fleet, not
  per-node). With `REDIS_URL` unset, the limiter falls back to in-process (single instance,
  or N× the effective ceiling if you run replicas without Redis). A transient Redis failure
  degrades to "allow" rather than blocking all traffic. Tune with `RATE_*_PER_MIN`.
- **Embedding/reranking workers**: if models are GPU-bound, run them on a dedicated
  GPU node pool and point `EMBED_MODEL`/`RERANK_MODEL` at a Text Embeddings Inference
  (TEI) endpoint instead of loading in-process. The embed/rerank interfaces accept a
  URL-based provider — wire `EMBED_BASE_URL` (TEI) and the same interface returns.

### Observability
- **Metrics**: scrape `GET /metrics` (Prometheus format) from each replica. Wire to
  Prometheus + Grafana. Key signals: `rag_request_duration_seconds`,
  `rag_requests_total`, `rag_ingest_jobs_total{status}`, `rag_ingest_chunks_total`,
  `rag_retrieval_duration_seconds`, `rag_query_hits`.
- **Logs**: structured JSON to stdout; ship to your log aggregator (Loki/ELK).
  Every ingestion/query/offboard event emits a `tenant_id`-tagged line.
- **Health**: `/health` (liveness) and `/health/ready` (Qdrant reachability) for
  probes.

### Tenant offboarding / churn
- `DELETE /api/v1/tenants/{id}` (admin) drops the tenant's Qdrant collection
  (hard data removal) and deletes the registry row. Idempotent and instant.

## 3. Kubernetes (optional path)
When you outgrow a single VPS: deploy Qdrant with the official Helm chart/Operator,
the app as a Deployment (HPA on CPU/QPS) behind a Service, Redis for limiter state,
and Prometheus Operator for scraping. The compose file is a 1:1 mapping of the same
services, so promotion is mechanical.
