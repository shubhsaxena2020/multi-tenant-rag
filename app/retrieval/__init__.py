"""Retrieval service: embed query -> vector search in tenant collection -> rerank.

Two-stage design (LocalAI Master 2026): broad vector recall (candidate_k) then a
cross-encoder reranker narrows to top_k. Falls back to score ordering if reranker is
disabled, so the API contract is stable regardless of model availability.

`retrieve` fetches the context chunks. If `generate=True`, the caller should pass the
result to `app.generation.generate_answer` (kept separate to avoid a hard LLM dep).
"""
from __future__ import annotations

from ..embed import get_embedder
from ..rerank import get_reranker
from ..vector_store import search


def retrieve(
    tenant_id: str,
    question: str,
    top_k: int = 8,
    candidate_k: int = 50,
    rerank: bool = True,
) -> list[dict]:
    embedder = get_embedder()
    reranker = get_reranker()
    qvec = embedder.embed_query(question)
    hits = search(tenant_id, qvec, limit=candidate_k)
    if rerank:
        hits = reranker.rerank(question, hits)
    return hits[:top_k]
