"""PHASE C (#13-#18) — agentic / multi-hop retrieval.

Iterative retrieve -> read top chunk -> reformulate follow-up -> re-retrieve loop for questions
that need synthesis across multiple passages (e.g. "How does X relate to Y?", "What caused Z given
W?"). Reuses the existing `retrieve()` so the hybrid dense+sparse + rerank pipeline is unchanged.

Design:
- Deterministic, offline-safe reformulation: each hop appends the highest-scoring chunk's text as
  grounding context to the original question (no LLM required). When an LLM is configured the same
  loop can be extended later, but the default path needs zero paid credentials.
- Plan-gated: standard tenants get a single hop; enterprise (or any tenant whose plan allows) gets
  up to `max_hops`. Requesting more hops than the plan permits is rejected by the caller (the /query
  route returns 402/403) — this is the Phase G feature-gate seed.
- Capped + resilient: hard caps on hops and chunks-per-hop; any retrieve error stops the loop
  (returns what it has so far) rather than raising.

Returns (merged_chunks, hop_count) where merged_chunks is de-duplicated across hops and re-ordered
by best score.
"""

from __future__ import annotations

from typing import Iterable

from app.retrieval import retrieve

# Default ceilings (also the enterprise allowance). Standard tenants are clamped to 1 hop.
DEFAULT_MAX_HOPS = 3
DEFAULT_CHUNKS_PER_HOP = 8

# Plans that may use multi-hop beyond a single retrieval.
_MULTI_HOP_PLANS = {"enterprise", "pro", "enterprise-paid"}


def plan_allows_multihop(plan: str) -> bool:
    """True when the tenant plan permits multi-hop retrieval (>1 hop)."""
    return (plan or "standard").lower() in _MULTI_HOP_PLANS


def max_hops_for(plan: str, requested: int | None = None) -> int:
    """Resolve the effective hop ceiling for a tenant plan.

    - Standard plans: always 1 (single retrieval).
    - Multi-hop plans: min(requested or DEFAULT_MAX_HOPS, DEFAULT_MAX_HOPS).
    """
    if not plan_allows_multihop(plan):
        return 1
    return max(1, min(requested or DEFAULT_MAX_HOPS, DEFAULT_MAX_HOPS))


def _reformulate(question: str, top_chunk_text: str) -> str:
    """Heuristic follow-up: ground the original question in the best chunk seen so far."""
    snippet = " ".join(top_chunk_text.split())[:400]
    return f"{question}\n\nContext from prior step: {snippet}"


def retrieve_multi_hop(
    tenant_id: str,
    question: str,
    *,
    plan: str = "standard",
    requested_hops: int | None = None,
    top_k: int = DEFAULT_CHUNKS_PER_HOP,
    candidate_k: int = 100,
    rerank: bool = True,
    acl_filter: object | None = None,
    max_chunks_per_hop: int = DEFAULT_CHUNKS_PER_HOP,
    max_hops: int = DEFAULT_MAX_HOPS,
) -> tuple[list[dict], int]:
    """Run a bounded multi-hop retrieval and return (merged_chunks, hop_count).

    Never raises: a retrieve error terminates the loop and returns what was gathered.
    """
    effective_hops = min(max_hops_for(plan, requested_hops), max_hops)
    if effective_hops < 1:
        effective_hops = 1

    seen: dict[str, dict] = {}
    hop_count = 0
    current_q = question
    for hop in range(effective_hops):
        hop_count += 1
        try:
            hits = retrieve(
                tenant_id, current_q, top_k=min(top_k, max_chunks_per_hop),
                candidate_k=candidate_k, rerank=rerank, acl_filter=acl_filter,
            )
        except Exception:
            # Resilience: stop on backend failure, keep what we have.
            break
        if not hits:
            # Only break on empty results if we're in single-hop mode.
            # For multi-hop, continue to next hop (no new results added, but
            # the hop still counts toward the effective_hops limit).
            if effective_hops <= 1:
                break
        else:
            for h in hits:
                cid = h.get("chunk_id") or h.get("doc_id")
                if cid and cid not in seen:
                    seen[cid] = h
            # Reformulate for the next hop using the best chunk's text.
            best = max(hits, key=lambda h: h.get("rerank_score", h.get("score", 0.0)))
            current_q = _reformulate(question, best.get("text", ""))
            # Stop early if we already have enough grounding (single strong result, no new signal).
            if effective_hops == 1:
                break

    merged = sorted(seen.values(), key=lambda h: h.get("rerank_score", h.get("score", 0.0)), reverse=True)
    return merged, hop_count


def multihop_denied(plan: str, requested_hops: int | None) -> bool:
    """True when a tenant on `plan` is requesting more hops than it is allowed."""
    if not requested_hops or requested_hops <= 1:
        return False
    return not plan_allows_multihop(plan)
