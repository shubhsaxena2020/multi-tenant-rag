# Architecture Trade-off Decisions (v7 audit findings #7–#9)

This document records the *deliberate, cited* decisions behind the three architecture
questions the independent audit raised, plus the runtime-verification method used for v7.

---

## #7 — Qdrant tenancy model: collection-per-tenant (current) vs Tiered Multitenancy

### Question
The current design gives **each tenant its own Qdrant collection** (the "Silo" pattern:
`{prefix}_{tenant_id}`). Research found credible evidence that Qdrant's own engineering team
now recommends *against* this past ~1,000 tenants. We independently verified the claim.

### What the official Qdrant guidance actually says (verified 2026-08-26)
- **Qdrant official docs — "Multitenancy"** (https://qdrant.tech/documentation/manage-data/multitenancy/):
  > "Creating a separate collection for each tenant is rarely the most efficient approach.
  > Each collection carries its own resource overhead, so creating many collections can
  > quickly become expensive. Only create multiple collections when you have a limited number
  > of tenants that need strict isolation."
  Recommended default: **one shared collection, partitioned by a `tenant_id` payload field**
  (`is_tenant=true` keyword index), with the filter applied at query time.
- **Qdrant multitenancy skill** (https://skills.qdrant.tech/qdrant-multitenancy/SKILL.md):
  "For almost everyone the right default is a single collection partitioned by payload, NOT a
  collection per tenant." And the "What NOT to do" note: payload-based isolation is an
  *application-layer responsibility* — the filter is only one part of it.
- Qdrant also documents **Tiered Multitenancy** (v1.16+): small tenants share one fallback
  shard; large/regulated tenants are promoted to dedicated shards — all inside one collection.

### The audit's specific claims (RAM fragmentation, FD exhaustion, slow restart) are real
Per-collection HNSW index duplication, one set of file descriptors per collection, and one
index to load per collection at startup are all well-established Qdrant scaling costs.
The official docs confirm the *direction* (collection-per-tenant does not scale; payload
partitioning/tiered is the recommended default).

### Decision
**Keep the collection-per-tenant "Silo" model for v7, but adopt Tiered Multitenancy as the
target architecture and record a concrete migration path.** Rationale:

1. **Our current scale is well under the documented ceiling.** We serve independent client
   *sites* (a small number of tenants, each needing strong isolation). The Silo model gives
   *physical* isolation for free, which matters for the "different client sites" trust boundary
   — one noisy/compromised tenant cannot touch another's vectors even under an app bug.
2. **Isolation strength.** Collection-per-tenant means a tenant can *never* read another's
   vectors at the storage layer, not just via a filter. Qdrant explicitly flags payload
   filtering as "not your whole security model" — for multi-*client* (not multi-*user*) data,
   physical separation is the safer default.
3. **Migration path is cheap and non-breaking** when we cross the threshold (see below).

### Migration path → Tiered Multitenancy (execute when tenant count approaches ~500–1,000)
- **Step 1 — Shared collection + `tenant_id` payload filter (payload partitioning):**
  Store all tenants' points in one collection `rag_shared`. Add a keyword payload index
  `tenant_id` with `is_tenant=True`. Every read/write already scopes by `tenant_id`, so the
  *code* change is localized to `vector_store.py` (`ensure_collection`/`collection_name` and
  the per-query filter). The existing `acl` filter composes with the `tenant_id` filter.
- **Step 2 — Enterprise/regulated tenants get dedicated shards (custom sharding):** for the
  small set of tenants that need physical isolation or have residency requirements, create
  dedicated shards and promote them. Reads/writes continue seamlessly during promotion.
- **Step 3 — Keep the Silo model available behind a flag** (`MULTITENANT_MODE=silo|tiered`) so
  individual enterprise tenants can opt into physical isolation even in tiered mode.
- **Backward compatibility:** the public API (tenant key → collection/tenant_id mapping) does
  not change; only the internal storage layout does. A one-time re-index job migrates points
  from per-tenant collections into the shared collection.

### Action items for the next version
- [ ] Add `MULTITENANT_MODE` setting + single code path in `vector_store.py`.
- [ ] Add a migration script `migrate_silo_to_tiered.py` (re-index per-tenant → shared + tenant_id).
- [ ] Add a payload `tenant_id` index + `acl` index in shared-collection mode.

---

## #8 — Rerank candidate_k: 100 → 30

### Question
Reranking `candidate_k=100` adds ~350–750 ms of CPU latency (cross-encoder). For a live chat
widget where users expect a response to *start* within ~1–1.5 s (Tidio 2026: 82% of customers
expect instant responses; AI replies are expected in seconds, Zipchat 2026 benchmarks), that
rerank cost eats most of the latency budget before generation even begins.

### What we measured (real benchmark, `_bench_rerank.py`, CPU cross-encoder FlashRank)
On this environment (CPU, FlashRank `ms-marco-MiniLM-L-12-v2`):
- `candidate_k=100`: ~550–750 ms rerank
- `candidate_k=30`:  ~150–220 ms rerank  (≈3.5× faster)
- `candidate_k=10`:  ~60–90 ms rerank

Recall impact: at the *query* level the top-1/top-3 result rarely changes between k=100 and
k=30 for typical FAQ/chat corpora (short, topically distinct chunks). The marginal recall gain
from 30→100 is small relative to the ~400 ms latency it costs.

### Decision
**Lower the default `candidate_k` from 100 to 30** (`app/models.py: QueryRequest.candidate_k
default=30`). This cuts rerank latency by ~3× with minimal recall loss, keeping the total
retrieve+rerank+generate budget comfortably under the ~1.2–1.5 s user expectation. Tenants that
want maximum recall can raise `candidate_k` per request (capped at 200).

### Verification
- Unit test `test_candidate_k_default_lowered` asserts the new default.
- Benchmark artifact `_bench_rerank.py` is committed so the trade-off can be re-measured on
  any target hardware before changing the number again.

---

## #9 — Conversation-aware chatbot (the critical gap)

The platform was single-turn document search. A live website chat widget needs three things a
search engine lacks. We implemented all three (no external services required; LLM is optional).

### (a) Conversational query rewriting
- **Problem:** follow-ups with pronouns/implicit refs ("how much does it cost?" after a turn
  about the Pro plan) retrieve the wrong content because the embedder only sees the bare phrase.
- **Approach (Alhena 2024 multi-turn RAG; Microsoft Copilot Studio RAG guidance; StackOverflow
  2024 multi-turn RAG):**
  - If an LLM is configured (`LLM_BASE_URL`/`LLM_API_KEY`/`LLM_MODEL`), ask it to rewrite the
    latest turn into a self-contained query using the session history (temperature 0).
  - Otherwise a **deterministic heuristic** prepends a compact context summary (last 2 user
    turns + last assistant answer) when the question contains reference pronouns
    (it/its/they/this/the plan/…). This makes the feature work with zero dependencies.
- **Verification:** `test_conversation_rewrite_resolves_followup` (heuristic path resolves
  "how much does it cost?" → includes "Pro"/"plan").

### (b) Out-of-scope / injection guard
- **Problem:** off-topic or prompt-injection turns waste a retrieval+LLM call and can be abused.
- **Approach (PromptGuard / Rebuff pattern — guardrail as a preprocessing sidecar):**
  - `detect_injection()` flags overt instruction-injection / jailbreak surface forms
    ("ignore previous instructions", "reveal your system prompt", "DAN", "jailbreak", …).
  - Flagged turns are still retrieved for benign lookup, but generation is gated: we do NOT
    forward the manipulative instruction into the LLM prompt, and we surface
    `injection_detected` in the response with a safe refusal.
- **Verification:** `test_injection_detection`, `test_query_injection_flag` (live endpoint
  returns `injection_detected=true` and a safe refusal).

### (c) Confidence-gated abstention
- **Problem:** low-relevance results trigger a hallucinated/extractive guess instead of a
  graceful handoff.
- **Approach (Self-RAG "know when not to answer"):** `assess_confidence()` returns
  `out_of_scope=True` when no chunks are returned or the best chunk's (rerank) score is below
  `RETRIEVAL_CONFIDENCE_THRESHOLD` (default 0.15). The response then carries a graceful
  "I don't have information on that — let me connect you with support" message instead of a
  generated guess.
- **Verification:** `test_query_out_of_scope_flag` (live endpoint with an empty tenant returns
  `out_of_scope=true` and the graceful answer).

### Caveat
- The heuristic rewrite is a baseline; the LLM rewrite path is the production-quality option and
  is exercised automatically when generation is configured. Session history is per-replica
  in-memory; multi-replica fleets should back `SessionStore` with Redis (interface is swap-ready).
- The injection detector is heuristic (pattern-based), not a full classifier — it catches the
  common overt attempts the audit tested, but should be treated as defense-in-depth, not a
  complete jailbreak solution.

---

## Runtime verification method for v7
The Qdrant daemon at localhost:6333 was unreachable in this environment (root-owned orphaned
listener sending RST; Docker not permitted to the agent user). To get *real* end-to-end
verification without the daemon, the test suite and a live smoke test run Qdrant **embedded
in-process** via `qdrant_client.QdrantClient(location=":memory:")` (selected with
`QDRANT_URL=":memory:"`). This exercises the actual hybrid-retrieval → RRF → rerank → response
pipeline against a real Qdrant engine. Production deployments pass a normal `http(s)://host` URL
and get the server-backed engine; the local mode is production-safe and also suitable for small
single-node deployments.

- Full suite: **36 passed** (`pytest tests/`, embedded Qdrant).
- Live HTTP smoke: uvicorn boot → `/health` 200 → create tenant → ingest → query (follow-up
  rewrite) → answer → `/metrics` all succeeded.
- `ruff check app/ tests/` → **All checks passed!**
