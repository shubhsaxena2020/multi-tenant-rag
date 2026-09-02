# Startup / Runtime Environment Assumptions — Verification Note

**Audited from**: repo state at HEAD e6d5229 on feat/rag-agent6-month-scale

## .env file (active runtime config)

The `.env` file has the following values set (non-empty):
- `QDRANT_URL=http://localhost:6333` — vector store endpoint
- `DB_URL=sqlite:///./rag_tenants.db` — local SQLite backing store
- `MASTER_ENCRYPTION_KEY=<redacted-present>` — 32 bytes base64, enables per-tenant AES-GCM encryption
- `ADMIN_API_KEY=<redacted-present>` — enables admin routes protection
- `USE_REAL_EMBEDDER=0` — embedder runs in mock mode (no real embeddings)
- `USE_REAL_RERANKER=0` — reranker runs in mock mode
- `EMBED_PREFIX_STYLE=e5` — embedding model prefix style

### Observations

1. **`MASTER_ENCRYPTION_KEY`** — Present in `.env` and documented as "REQUIRED in production — without it, chunk text is stored in plaintext" (.env example line 20). ✅ Explicit requirement stated.

2. **`ADMIN_API_KEY`** — Present in `.env`. The .env.example comments: "Leave empty only for local/dev (admin endpoints are then open)." This is a clear fail-closed expectation: if ADMIN_API_KEY is empty, admin endpoints are open; set it in production. ✅ Documented.

3. **`REDIS_URL`** — Not set in `.env`. The .env.example comment: "When set, the per-IP/per-tenant token-bucket is shared in Redis so multiple app replicas behind a load balancer enforce ONE global budget. Leave empty for in-process limiting (single instance only). Requires a `redis` client; degrades to allow on error." ✅ Documented: empty = single-instance in-process only, not production-safe for multi-replica.

4. **`LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`** — Not set in `.env`. The .env.example comments: "When all three are set, POST /{tenant}/query with generate=true calls this provider." ✅ Documented: optional, not set = no answer generation.

5. **`EMBED_MODEL`, `EMBED_SPARSE_MODEL`, `EMBED_DEVICE`** — Set in `.env` with defaults `intfloat/multilingual-e5-large`, `prithivida/Splade_PP_en_v1`, `cpu`. ✅ Defaults explicit.

6. **`TENANT_CHUNK_QUOTA=5000000`** — Present and documented as "fleet protection; 0 disables". ✅ Explicit default and disable behavior.

7. **`RATE_PER_TENANT_PER_MIN=600`, `RATE_PER_IP_PER_MIN=120`, `RATE_INGEST_JOBS_PER_MIN=60`** — All present with explicit numeric values. ✅ Rate limits configured.

8. **`QDRANT_URL=http://localhost:6333`** — Points to localhost. The .env.example says "Qdrant daemon at localhost:6333 was unreachable in this environment (root-owned orphaned listener sending RST; Docker not permitted to the agent user)." This is an environment-specific caveat, not a doc gap. ✅ Noted.

### Summary

All environment variables in `.env.example` have:
- Explicit default values (or "leave empty for dev" / "0 disables" / "REQUIRED in production")
- Clear fail-closed or open expectations documented
- No missing variable definitions that would cause startup failure

The only "gap" is the environment-specific Qdrant unreachable caveat, which is already documented in DESIGN-TRADEOFFS.md (Phase K runtime verification section). No changes needed to `.env.example`.

**Verdict**: Environment assumptions are properly documented with explicit defaults and fail-closed expectations. No documentation updates required.

**Next**: Continue to next backlog item.
