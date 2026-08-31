# Task 8: Vector-Store and Quota Triage for Larger Corpora

## Overview

This document triages the vector-store and quota implications when scaling
to larger document corpora in `rag-service`. It identifies current
limitations, bottlenecks, and recommended mitigation strategies.

## Vector-Store Current Behavior

### `upsert_chunks()` — Ingestion Path

**Function signature:**
```python
upsert_chunks(
    tenant_id: str,
    doc_id: str,
    title: str,
    chunks: list[str],
    dense: list[list[float]],
    sparse: list[dict[int, float]],
    base_metadata: dict[str, Any],
    content_type: str,
) -> list[str]
```

**How it works:**
1. Ensures the Qdrant collection exists via `ensure_collection()`
2. Builds `PointStruct` objects with:
   - Dense vector (`"": dvec`)
   - Sparse vector (`"text": _sparse_vec(svec)`)
   - Encrypted payload text (`crypto.encrypt_text(tenant_id, text)`)
   - Tenant/doc metadata (`tenant_id`, `doc_id`, `title`, `chunk_index`, etc.)
3. Batches upserts in groups of `_BATCH` (default batch size)
4. Final partial batch is upserted if non-empty

**Key observations for large corpora:**
- **Batch size (`_BATCH`):** Controls upsert throughput. Larger batches = fewer API calls
  but more memory per call. Default needs evaluation for corpus scale.
- **PointStruct per chunk:** Each chunk generates a separate Qdrant point.
  For 10,000 documents × 100 chunks each = 1,000,000 points.
- **Encryption overhead:** `crypto.encrypt_text(tenant_id, text)` runs per chunk,
  adding CPU cost at ingestion time.
- **Collection growth:** Qdrant collections grow linearly with total chunks.
  No automatic compaction or TTL — must manage manually.

### `search_dense()` — Query Path

**Function signature:**
```python
search_dense(
    tenant_id: str,
    vector: list[float],
    limit: int = 10,
    score_threshold: float | None = None,
    acl_filter: Filter | None = None,
) -> list[dict[str, Any]]
```

**Key observations:**
- Single-tenant scope requires `tenant_id` on every search
- `score_threshold` enables early termination for relevance filtering
- `acl_filter` supports row-level access control (see `app/auth.py`)
- No pagination parameter — returns top `limit` results only

## Quota and Rate-Limit Infrastructure

### Current State

| Component | Status | Notes |
|---|---|---|
| `rate_limit()` | Exists but global-ish | Single global limit, not per-tenant |
| `tenant_chunk_quota` | Column added to `tenants` table (via migration) | Tracks chunks per tenant |
| `get_tenant_chunk_quota()` | Available | Returns tenant's quota limit |
| `increment_chunk_count()` | Available | Increments tenant's chunk counter |
| 429 / Retry-After | Implemented (see Phase B per-tenant quotas) | Returns 429 when over quota |

### BACKLOG.md Items Related to Quotas

- Item 31: `[-] **Per-tenant rate limiting / quotas**: rate_limit() exists but
  is global-ish; add per-tenant quotas (requests/min, chunks ingested, collection size)
  to prevent noisy neighbors.`
- Item 32: `[-] **Per-tenant quotas (requests/min, chunks ingested, collection size)**
  to prevent noisy neighbors.`
- Item 42: `[-] **Audit `rate_limit()` and record the per-tenant quota gap as a
  tracked item (see Phase I).`
- Item 43: `[-] **Store per-tenant quota counters (req/min, chunks ingested,
  collection size) in the tenant registry; return 429 with Retry-After.`**
- Item 46: `[-] **Commit, PR, merge, tag `v16.55-tenant-quotas`.``

## Triage: Issues Identified for Larger Corpora

### Issue 1: Batch Size Optimization

**Problem:** The `_BATCH` constant controls upsert batching but its value
needs evaluation for large-scale ingestion.

**Impact:** Small batches → many API calls, slow ingestion. Large batches
→ memory pressure, potential Qdrant timeouts.

**Recommendation:**
- Profile ingestion with realistic corpus sizes (1K, 10K, 100K chunks)
- Tune `_BATCH` to 64-128 for typical workloads, adjust based on Qdrant
  instance capacity
- Make `_BATCH` configurable per deployment (environment variable or
  settings object)

### Issue 2: Quota Enforcement Gaps

**Problem:** While quota counter infrastructure exists (`tenant_chunk_quota`
column, `increment_chunk_count()`, `get_tenant_chunk_quota()`), the
ingestion path (`upsert_chunks()`) does not check quotas before writing.

**Impact:** Tenants can exceed their chunk quotas silently — no 429
enforcement during active ingestion.

**Recommendation:**
- Add quota check at the start of `upsert_chunks()`:
  ```python
  current_count = get_tenant_chunk_quota(tenant_id)
  if current_count + len(chunks) > quota_limit:
      raise HTTPException(429, "Tenant chunk quota exceeded")
  ```
- Integrate with existing rate-limit middleware for consistent 429 handling
- Ensure `increment_chunk_count()` is called atomically after successful upsert

### Issue 3: Collection Growth and Memory

**Problem:** Qdrant collections grow linearly with total chunks. No TTL,
compaction, or deletion mechanism for stale/chunked data.

**Impact:** Unchecked growth leads to increased storage costs, slower
search, and potential Qdrant performance degradation.

**Recommendation:**
- Implement chunk deletion API (remove stale/old chunks)
- Add collection TTL or archival strategy (export to cold storage, delete
  from active collection)
- Periodic audit of chunk retention policies per tenant

### Issue 4: Sparse Vector Index Scaling

**Problem:** Sparse vectors use Qdrant's built-in indexing (`"text": _sparse_vec(svec)`).
Performance characteristics degrade as the collection grows without
proper index configuration.

**Impact:** Search latency increases as collection size grows — from
milliseconds to seconds over 100K+ points without re-indexing.

**Recommendation:**
- Profile search performance at scale (10K, 100K, 1M points)
- Configure Qdrant sparse vector index parameters (`on_disk` vs `in_memory`)
- Consider hierarchical retrieval: sparse → dense → re-ranking pipeline

### Issue 5: Multi-Tenant Isolation at Scale

**Problem:** Every operation requires `tenant_id` filtering. No tenant-aware
connection pooling or query routing in the vector store.

**Impact:** Growing number of tenants → increased metadata overhead per query,
potential cross-tenant contamination if filters are omitted.

**Recommendation:**
- Ensure all `upsert_chunks()`, `search_dense()`, `search_hybrid()`, and
  `search_sparse()` always include `tenant_id` in payload/filter
- Consider tenant-sharded collections for extreme scale (separate Qdrant
  collection per tenant)
- Add tenant quota monitoring dashboard

## Mitigation Priority Matrix

| Issue | Impact | Effort | Priority |
|---|---|---|---|
| Batch size optimization | Medium | Low | P1 (profiling first) |
| Quota enforcement gaps | High | Medium | P1 (critical for multi-tenant) |
| Collection growth | Medium | Medium | P2 (future-proofing) |
| Sparse vector indexing | Medium | Medium | P2 (performance at scale) |
| Multi-tenant isolation | High | Low | P1 (already have tenant_id, just need consistency) |

## Related BACKLOG.md Items

- Item 31: `[-] **Per-tenant rate limiting / quotas**: rate_limit() exists but
  is global-ish; add per-tenant quotas (requests/min, chunks ingested, collection size)
  to prevent noisy neighbors.`
- Item 32: `[-] **Per-tenant quotas (requests/min, chunks ingested, collection size)**
  to prevent noisy neighbors.`
- Item 42: `[-] **Audit `rate_limit()` and record the per-tenant quota gap as a
  tracked item (see Phase I).`
- Item 43: `[-] **Store per-tenant quota counters (req/min, chunks ingested,
  collection size) in the tenant registry; return 429 with Retry-After.`**
- Item 50: `[-] **Commit, PR, merge, tag `v16.56-ingestion-scale`.``

## Recommended Next Steps

1. **Profile ingestion:** Run `upsert_chunks()` with 1K, 10K, 100K chunks and
   measure throughput, memory, and Qdrant API call count
2. **Tune `_BATCH`:** Based on profiling results, set optimal batch size
3. **Add quota checks:** Insert quota enforcement at the `upsert_chunks()`
   entry point with proper 429 response
4. **Implement deletion:** Add `delete_chunks()` or `remove_tenant()` for
   stale data management
5. **Profile search:** Test `search_dense()` latency at 10K/100K/1M points
   and configure sparse vector index accordingly
6. **Add monitoring:** Dashboard for tenant chunk counts, quota utilization,
   and search latency per tenant