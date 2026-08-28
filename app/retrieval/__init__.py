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
from ..observability import get_logger
from ..rerank import get_reranker

log = get_logger("rag")


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
    # Query/document prefixing (e.g. E5 query:/passage:) is handled centrally by the
    # embedder so passages (ingestion) and queries stay consistent (v8 #4).
    q = embedder.embed_query(question)

    # Use native Qdrant hybrid search with prefetch + server-side fusion
    # This replaces the client-side RRF fusion for better performance and accuracy
    from qdrant_client.models import Filter

    from ..vector_store import search_hybrid

    # Convert acl_filter to Qdrant Filter if needed
    qdrant_acl_filter: Filter | None = None
    if acl_filter is not None:
        # If it's already a Qdrant Filter from build_acl_filter, use it directly
        if isinstance(acl_filter, Filter):
            qdrant_acl_filter = acl_filter
        else:
            # Handle case where it might be a list (backward compatibility)
            from ..rbac import build_acl_filter
            if isinstance(acl_filter, list):
                qdrant_acl_filter = build_acl_filter(acl_filter)

    fused = search_hybrid(
        tenant_id=tenant_id,
        dense_vector=q.dense,
        sparse_vector=q.sparse,
        limit=top_k,
        candidate_k=candidate_k,
        fusion_method="rrf",  # Use RRF as default to match previous behavior
        acl_filter=qdrant_acl_filter,
    )

    if rerank:
        try:
            fused = reranker.rerank(question, fused)
        except Exception as e:
            # Degrade gracefully: if the real reranker fails (model load, OOM), fall
            # back to the deterministic score sort so the chatbot still answers instead
            # of 500-ing. Log the type only (never the raw traceback).
            log.warning("reranker_fallback", extra={"error_type": type(e).__name__})
            from ..rerank import ScoreReranker

            fused = ScoreReranker().rerank(question, fused)
    return fused
