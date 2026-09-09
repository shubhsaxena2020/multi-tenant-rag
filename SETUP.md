# Setup — from `git clone` to a working multi-tenant RAG in ~15 minutes

This repo is a **template**. Clone it, run one command, and you have a self-hosted,
multi-tenant Retrieval-Augmented Generation service for your own project. There is
nothing to install outside this repo except Docker.

> **For an AI agent doing this setup:** follow "One-command setup" below. Every step is a
> real shell command. Verify each with the check listed. If `setup.sh` fails, fall back to
> "Manual setup" and report exactly which step failed with its output.

---

## Prerequisites

| Need | Check | If missing |
|---|---|---|
| Docker Engine or Docker Desktop | `docker info` | install Docker, start the daemon |
| Docker Compose v2 | `docker compose version` | ships with Docker Desktop; else `apt install docker-compose-plugin` |
| `curl` | `curl --version` | `apt install curl` / preinstalled on macОС |
| ~4 GB free RAM, ~2 GB disk | — | more if you enable real models |

Python is **not** required on the host — the service runs entirely in containers.
`openssl` is used for secret generation; if absent, `setup.sh` falls back to `python3`.

---

## One-command setup

```bash
git clone <this-repo-url> rag-service
cd rag-service
./setup.sh
```

`setup.sh` will:

1. check Docker + Compose are usable;
2. create `.env` from `.env.example`, generating a fresh `ADMIN_API_KEY` and
   `MASTER_ENCRYPTION_KEY` (it **never** overwrites an existing `.env`);
3. `docker compose up -d` — starts **Qdrant** (vector DB) and the **API**;
4. poll `http://localhost:8000/health` until it is `ok`;
5. create your first tenant and print its `tenant_id` + secret `api_key`;
6. print copy-paste `curl` commands for ingest / query / publishable-key, plus the
   widget embed snippet and the dashboard URLs.

Useful flags:

| Flag | Effect |
|---|---|
| `--with-monitoring` | also start Prometheus + Alertmanager + Grafana (`:9090` / `:9093` / `:3000`) |
| `--with-redis` | also start Redis so multiple API replicas share one rate-limit budget |
| `--real-models` | put `USE_REAL_EMBEDDER=1` / `USE_REAL_RERANKER=1` in a *new* `.env` (first boot downloads ~2 GB) |
| `--port 9000` | map the API to a different host port |
| `--no-tenant` | skip creating the first tenant |

**Check it worked:**

```bash
curl -s localhost:8000/health                 # {"status":"ok",...}
curl -s localhost:8000/health/ready           # {"status":"ready","qdrant":"reachable"}
```

---

## What you get

| Container | Port | What |
|---|---|---|
| `rag-service-app-1` | 8000 | the FastAPI service (all `/api/v1` routes) |
| `rag-service-qdrant-1` | 6333 / 6334 | Qdrant vector DB (one collection per tenant) |
| *(monitoring profile)* prometheus / alertmanager / grafana | 9090 / 9093 / 3000 | metrics, alerts, dashboards |
| *(ratelimit profile)* redis | 6379 | shared rate-limit state |

Surfaces:

- `http://localhost:8000/api/v1/docs` — interactive Swagger UI for every endpoint
- `http://localhost:8000/demo` — a demo chat page
- `http://localhost:8000/admin/console` — admin console
- `http://localhost:8000/metrics` — Prometheus exposition

By default the service uses **deterministic placeholder models** — the full hybrid
retrieval + rerank pipeline runs with **zero model downloads**, so it boots in seconds and
the test suite passes offline. Flip to real models when you're ready (see below).

---

## Manual setup (same steps, by hand)

```bash
# 1. secrets
cp .env.example .env
# edit .env and set:
#   ADMIN_API_KEY=<a strong random string>          # gates tenant create/list/delete
#   MASTER_ENCRYPTION_KEY=$(openssl rand -base64 32) # encrypts chunk text at rest

# 2. start
docker compose up -d                    # qdrant + app
# docker compose --profile monitoring up -d   # optional: prometheus/grafana
# docker compose --profile ratelimit up -d    # optional: redis

# 3. wait for health
until curl -fsS localhost:8000/health >/dev/null; do sleep 2; done

# 4. create a tenant (needs the admin key you set in .env)
curl -X POST localhost:8000/api/v1/tenants \
  -H "Admin-Key: $ADMIN_API_KEY" -H 'Content-Type: application/json' \
  -d '{"name":"my-first-tenant","plan":"standard"}'
# → {"tenant_id":"t_xxxx","api_key":"rk_xxxx", ...}   ← save both
```

---

## Your first tenant → ingest → query
### Document-level RBAC

New tenants use allowed_groups=["__public__"]. Documents tagged with an ACL group
are not returned to callers that omit acl unless that group has been explicitly
provisioned for the tenant. Pass an allowed group in the query, or update an existing
tenant as an operator:

```bash
curl -X PATCH localhost:8000/api/v1/tenants/$TID \
  -H "Admin-Key: $ADMIN_API_KEY" -H 'Content-Type: application/json' \
  -d '{"allowed_groups":["support","billing"]}'
```

Set allowed_groups=["*"] explicitly only for a tenant that should retain unrestricted
backward-compatible wildcard access; existing tenant rows are not changed automatically.


```bash
TID=t_xxxx            # from the create-tenant response
SK=rk_xxxx            # the secret key from the same response
AUTH="Authorization: Bearer $SK"

# ingest text
curl -X POST localhost:8000/api/v1/$TID/ingest/text -H "$AUTH" \
  -H 'Content-Type: application/json' \
  -d '{"title":"Handbook","text":"Acme builds onboarding software. Support is support@acme.example."}'

# ingest a web page (SSRF-checked fetch)
curl -X POST localhost:8000/api/v1/$TID/ingest/url -H "$AUTH" \
  -H 'Content-Type: application/json' -d '{"url":"https://example.com","title":"Example"}'

# ingest a file
curl -X POST localhost:8000/api/v1/$TID/documents/upload -H "$AUTH" -F 'file=@handbook.pdf'

# large / slow source → async job, then poll
JOB=$(curl -s -X POST localhost:8000/api/v1/$TID/ingest/jobs -H "$AUTH" \
  -H 'Content-Type: application/json' -d '{"kind":"text","title":"a","text":"..."}' | \
  sed -n 's/.*"job_id":"\([^"]*\)".*/\1/p')
curl -s localhost:8000/api/v1/$TID/jobs/$JOB -H "$AUTH"     # status → completed

# query
curl -X POST localhost:8000/api/v1/$TID/query -H "$AUTH" \
  -H 'Content-Type: application/json' \
  -d '{"question":"how do teams onboard with Acme?","top_k":5,"rerank":true}'

# query with an LLM-written answer (needs LLM_* set in .env)
curl -X POST localhost:8000/api/v1/$TID/query -H "$AUTH" \
  -H 'Content-Type: application/json' \
  -d '{"question":"...","top_k":5,"generate":true}'
```

**Document-level access control:** tag chunks on ingest with `"acl":["group"]`, then scope
a query with `"acl":["group"]` — only chunks whose groups intersect are retrieved. The
filter is applied inside Qdrant and cannot be bypassed.

---

## Embedding the chat widget

Issue a **publishable** (read-only, browser-safe) key:

```bash
curl -X POST localhost:8000/api/v1/$TID/keys/publishable -H "Authorization: Bearer $SK"
# → {"api_key":"pk_xxxx", ...}
```

Drop this on any page:

```html
<script src="http://localhost:8000/widget.js"
        data-tenant="t_xxxx"
        data-key="pk_xxxx"></script>
```

Customise per tenant:

```bash
curl -X PATCH localhost:8000/api/v1/$TID/branding -H "$AUTH" \
  -H 'Content-Type: application/json' \
  -d '{"header_title":"Acme Help","accent":"#4f46e5"}'
curl -X PATCH localhost:8000/api/v1/$TID/system-prompt -H "$AUTH" \
  -H 'Content-Type: application/json' \
  -d '{"system_prompt":"You are Acme support. Be concise."}'
```

---

## Using the SDKs

**Python** (`sdk.py`):

```python
from sdk import RagClient
c = RagClient("http://localhost:8000", tenant_id="t_xxxx", api_key="rk_xxxx")
c.ingest_text("Handbook", "Acme builds onboarding software.")
print(c.query("what does Acme do?", top_k=3))
```

**JavaScript / TypeScript** (`sdk-js/`):

```ts
import { RagClient } from "rag-service-sdk";
const c = new RagClient({ baseUrl: "http://localhost:8000", tenantId: "t_xxxx", apiKey: "rk_xxxx" });
await c.ingestText("Handbook", "Acme builds onboarding software.");
console.log(await c.query("what does Acme do?", { topK: 3 }));
```

---

## Configuration (`.env`)

Only the first two matter for a first run; the rest have sensible defaults
(full list + comments in `.env.example`, definitions in `app/config.py`).

**Auth & encryption**
- `ADMIN_API_KEY` — required to create/list/delete tenants (leave empty only for throwaway local use)
- `MASTER_ENCRYPTION_KEY` — 32 bytes (base64/hex); without it, chunk text is stored in plaintext

**Vector store**
- `QDRANT_URL` (default `http://qdrant:6333` inside compose), `COLLECTION_PREFIX` (default `rag`), `VECTOR_SIZE` (1024)

**Embedding / rerank**
- `USE_REAL_EMBEDDER` / `USE_REAL_RERANKER` — `0` = deterministic (default), `1` = load real models
- `EMBED_PROVIDER` (`fastembed` | `sentence_transformers`), `EMBED_MODEL`, `EMBED_SPARSE_MODEL`
- `RERANK_PROVIDER` (`flashrank` | `sentence_transformers`), `RERANK_MODEL`
- `EMBED_BASE_URL` — optional TEI GPU node to offload embedding

**Limits (fleet safety)**
- `TENANT_CHUNK_QUOTA` (5,000,000), `RATE_PER_TENANT_PER_MIN` (600), `RATE_PER_IP_PER_MIN` (120), `RATE_INGEST_JOBS_PER_MIN` (60)
- `REDIS_URL` — set (and start `--with-redis`) to share limits across replicas

**Answer generation (optional)**
- `LLM_BASE_URL` + `LLM_API_KEY` + `LLM_MODEL` — any OpenAI-compatible endpoint; enables `generate=true`

Apply changes: edit `.env`, then `docker compose up -d`.

---

## Turning on real models

```bash
# in .env
USE_REAL_EMBEDDER=1
USE_REAL_RERANKER=1
# optionally the full BGE-M3:
EMBED_PROVIDER=sentence_transformers
EMBED_MODEL=BAAI/bge-m3
```
```bash
docker compose up -d      # first request downloads the weights (~2 GB), then cached
```
Offload embedding to a GPU box instead: run a
[Text Embeddings Inference](https://github.com/huggingface/text-embeddings-inference)
server and set `EMBED_BASE_URL=http://that-host:8080`.

---

## Running the tests

```bash
docker compose exec app python -m pytest -q
# or on the host, in a venv: pip install -e ".[test]" && pytest -q
```
The suite uses deterministic models and an in-process store, so most of it needs no
running services.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `setup.sh`: "docker daemon not reachable" | start Docker Desktop / `sudo systemctl start docker` |
| `/health` never turns `ok` | `docker compose logs app` — usually a bad `.env` value or port 8000 already in use (`./setup.sh --port 9000`) |
| tenant create → `403` | `ADMIN_API_KEY` in `.env` doesn't match the `Admin-Key` header you sent; re-source `.env` |
| `/health/ready` → qdrant not reachable | `docker compose restart qdrant`; check `docker compose ps` shows it healthy |
| queries return nothing | you haven't ingested for that tenant yet, or the `acl` on the query doesn't match any chunk's groups |
| real-model boot is slow / OOM | first download is ~2 GB; give the `app` container ≥3 GB, or use `EMBED_BASE_URL` |
| want a clean slate | `docker compose down -v` (drops Qdrant data), delete `.env`, re-run `./setup.sh` |

---

## What this is (and isn't)

It **is** a self-hostable multi-tenant RAG service you stand up per project: many isolated
tenants, per-tenant keys / quotas / Qdrant collections, document-level RBAC,
encryption-at-rest, hybrid retrieval + rerank, async ingestion, an eval harness, and a
Prometheus/Grafana stack.

It is **not** a hosted SaaS you operate for external customers. There is no signup portal,
no billing, no per-user password store — "tenants" are API namespaces you create with your
admin key. Clone it, run it, point it at your own docs.

See `README.md` for the architecture and `docs/` for operator runbooks.
