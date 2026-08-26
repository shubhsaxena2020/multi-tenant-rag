"""Retrieval service — hybrid (dense + sparse) → RRF fusion → cross-encoder rerank.

Production pipeline (LocalAI Master 2026 + Agile Infoways 2026):
  1. Embed query → dense + sparse (BGE-M3 one pass).
  2. Dense ANN recall (candidate_k) in the tenant's isolated collection.
  3. Sparse/lexical recall (candidate_k) in the same collection.
  4. Reciprocal Rank Fusion (RRF) merges both result lists into one ranked set —
     hybrid lifts recall@10 by ~8–14 pts vs dense-only, and recovers literal matches
     (part numbers, codes) that dense misses.
  5. Cross-encoder reranker narrows fused candidates to top_k (default ON).

Isolation is preserved: every query is scoped to the tenant collection; an optional
ACL filter (document-level RBAC) is applied at the DB layer, never in app logic that
could be bypassed.
"""
from __future__ import annotations

from ..embed import get_embedder
from ..rerank import get_reranker
from ..vector_store import search_dense, search_sparse


def _rrf(results_lists: list[list[dict]], k: int = 60) -> list[dict]:
    """Reciprocal Rank Fusion across dense + sparse result lists."""
    fused: dict[str, dict] = {}
    for results in results_lists:
        for rank, hit in enumerate(results):
            cid = hit["chunk_id"]
            if cid not in fused:
                fused[cid] = dict(hit)
                fused[cid]["_rrf"] = 0.0
            fused[cid]["_rrf"] += 1.0 / (k + rank + 1)
    ordered = sorted(fused.values(), key=lambda h: h["_rrf"], reverse=True)
    return ordered


def retrieve(
    tenant_id: str,
    question: str,
    top_k: int = 8,
    candidate_k: int = 100,
    rerank: bool = True,
    acl_filter: object | None = None,
) -> list[dict]:
    embedder = get_embedder()
    reranker = get_reranker()
    q = embedder.embed_query(question)

    # Parallel-ish dense + sparse recall (both scoped to tenant collection)
    dense_hits = search_dense(tenant_id, q.dense, candidate_k, acl_filter=acl_filter)
    sparse_hits = search_sparse(tenant_id, q.sparse, candidate_k, acl_filter=acl_filter)

    fused = _rrf([dense_hits, sparse_hits]) if sparse_hits else dense_hits
    if rerank:
        fused = reranker.rerank(question, fused)
    return fused[:top_k]
