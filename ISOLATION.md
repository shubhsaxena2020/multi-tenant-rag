# Tenant Isolation Strategy

This service is multi-tenant by design: many client sites share one deployment but
their documents, embeddings, and retrieval results must never leak into another
client's space — even if application code has a bug.

## Threat model
- A chatbot for client A must never receive chunks derived from client B's data.
- A bug in query routing, a forgotten filter, or a confused API caller must not
  surface cross-tenant content.
- Offboarding a client must remove their data completely and promptly.

## Two canonical patterns (and why we chose one)

### Pool (logical isolation on shared collection)
All tenants live in one (or a few) collections; every point carries a `tenant_id`
payload; queries append a `tenant_id` filter.
- Pros: cheap, scales to 10k+ tenants with linear cost (no per-tenant index),
  instant onboarding (no provisioning), trivial cross-tenant analytics if ever needed.
- Cons: isolation is enforced by *application logic* + query filter. A missing filter,
  a filter-wiring bug, or a tenant-supplied `tenant_id` in the body becomes a
  data-exfiltration path. HNSW memory is shared, which is good for cost but means one
  noisy tenant affects others' tail latency.

### Silo (physical isolation: per-tenant collection)  ← CHOSEN FOR v1
Each tenant gets its own Qdrant collection named `{prefix}_{tenant_id}`. All reads and
writes are scoped to that collection.
- Pros: isolation is **structural**, not procedural. There is no code path by which a
  query against collection `rag_tenantA` can return `tenantB` points, because the
  collection simply does not contain them. Offboarding = `DELETE COLLECTION`
  (instant, complete). Per-tenant quotas, KMS keys, and lifecycle policies are native.
- Cons: one HNSW index per tenant held in RAM; idle tenants still cost memory. At
  very high tenant counts (thousands) this is wasteful and can hit collection limits.

## Decision
v1 uses **Silo** because the core requirement is *hard* isolation with zero reliance on
application-layer filtering. The isolation guarantee must hold "even under a bug"
(requirement). Silo makes cross-tenant reads impossible at the storage layer, which is
the only guarantee strong enough.

## Defense-in-depth (applied here)
1. **Tenant identity is server-side.** The API key in the `Authorization` header is
   resolved to `tenant_id` via `auth.get_tenant_from_header`. The `tenant` path
   segment is informational only; the authoritative tenant always comes from the key,
   never from the request body. A client cannot spoof a tenant id.
2. **Every vector op is scoped to `tenant_{id}`.** `vector_store.py` derives the
   collection name from the resolved tenant; there is no global "search everything"
   function.
3. **Graceful empty on missing collection.** If a collection does not exist, search/
   delete return empty rather than erroring — this never widens the result set.
4. **API keys stored hashed** (SHA-256) so a DB leak does not hand out live keys.

## Migration path (documented, not yet implemented)
When a deployment exceeds ~a few hundred tenants or memory pressure from idle
collections becomes significant, migrate SMB/mid-market tenants to **Pool** mode
(shared collection + mandatory `tenant_id` payload filter enforced server-side), while
keeping **enterprise** tenants on Silo (per the hybrid pattern in the 2026 Truto
multi-tenant RAG guide). The public API does not change; only the storage mapping
inside `vector_store.py` changes.

## Source
Truto (2026), "Multi-Tenant RAG Data Isolation: The 2026 Enterprise Architecture
Guide" — isolation must be deterministic at the vector database, not "we filter at the
LLM layer" (which it explicitly calls security theater).
