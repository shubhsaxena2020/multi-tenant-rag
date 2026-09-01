"""PHASE D (#19-#24) — citation faithfulness & no-answer detection.

Scores whether a generated answer is grounded in the retrieved context so we can (a) surface a
`faithfulness` (0..1) + `answerable` flag to callers, (b) log the distribution per tenant, and
(c) drive a safe no-answer ("I don't know") path instead of hallucinated text.

Deterministic by default: a token-overlap heuristic (what fraction of the answer's salient tokens
appear in the context) gives a robust 0..1 score with zero paid credentials. An optional LLM
self-check can be layered on top when `LLM_BASE_URL` is configured (mockable in tests) — if it is
unavailable or errors, we fall back to the deterministic score so the path never breaks.
"""

from __future__ import annotations
import re
from typing import Iterable

_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "for", "with", "is", "are",
    "was", "were", "be", "been", "being", "it", "this", "that", "these", "those", "as", "at",
    "by", "from", "we", "you", "they", "he", "she", "i", "our", "your", "their", "its", "can",
    "could", "should", "would", "will", "may", "might", "do", "does", "did", "has", "have",
    "had", "yes", "if", "then", "than", "so", "there", "here", "about", "into",
    "over", "under", "between", "both", "each", "more", "most", "other", "some", "such", "only",
}

def _normalize_token(t: str) -> str:
    """Strip possessive 's and standardize common contractions for token overlap."""
    # Strip trailing possessive 's (e.g., "dog's" -> "dog")
    if t.endswith("'s"):
        t = t[:-2]
    return t

def _tokens(text: str) -> list[str]:
    raw_tokens = [t for t in re.findall(r"[a-z0-9][a-z0-9'-]*", text.lower()) if t not in _STOPWORDS and len(t) > 1]
    return [_normalize_token(t) for t in raw_tokens]


def is_refusal(answer: str | None) -> bool:
    """Detect a safe no-answer / refusal turn (so we don't treat it as a grounded answer)."""
    if not answer:
        return True
    low = answer.lower()
    markers = (
        "i don't know", "i do not know", "i cannot", "i can't",
        "no information", "don't have information", "do not have information",
        "i'm sorry", "i am sorry", "unable to", "can't help", "cannot help",
    )
    return any(m in low for m in markers)


def token_overlap(answer: str, context: str) -> float:
    """Fraction of the answer's salient tokens that are present in the context (0..1).

    Uses recall-oriented coverage: how much of the answer is supported by the retrieved text.
    """
    ans_tokens = _tokens(answer)
    if not ans_tokens:
        return 0.0
    ctx_tokens = set(_tokens(context))
    if not ctx_tokens:
        return 0.0
    supported = sum(1 for t in ans_tokens if t in ctx_tokens)
    return supported / len(ans_tokens)


def _llm_self_check(answer: str, context: str) -> float | None:
    """Optional LLM faithfulness verdict (1.0 grounded / 0.0 not). Returns None if no LLM."""
    from app.config import get_settings
    try:
        s = get_settings()
        if not (s.llm_base_url and s.llm_api_key and s.llm_model):
            return None
        import httpx
        resp = httpx.post(
            s.llm_base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {s.llm_api_key}", "Content-Type": "application/json"},
            json={
                "model": s.llm_model,
                "messages": [{"role": "user", "content": (
                    "Answer strictly grounded? Reply with only 'yes' or 'no'.\n"
                    f"CONTEXT:\n{context[:2000]}\n\nANSWER:\n{answer[:1000]}"
                )}],
                "temperature": 0.0,
            },
            timeout=20,
        )
        resp.raise_for_status()
        verdict = resp.json()["choices"][0]["message"]["content"].strip().lower()
        if verdict.startswith("yes"):
            return 1.0
        if verdict.startswith("no"):
            return 0.0
        return None
    except Exception:
        return None


def score_faithfulness(
    answer: str | None,
    context_chunks: Iterable[str],
    *,
    use_llm: bool = True,
) -> tuple[float, bool]:
    """Return (faithfulness 0..1, answerable).

    - Empty / refusal answer -> (0.0, False).
    - Otherwise a deterministic token-overlap score, optionally nudged by an LLM self-check when
      configured (and only when the LLM strongly disagrees). Never raises.
    """
    if is_refusal(answer):
        return 0.0, False
    context = " ".join(context_chunks)
    score = token_overlap(answer or "", context)
    if use_llm:
        llm = _llm_self_check(answer or "", context)
        if llm is not None:
            # Trust a strong LLM disagreement; otherwise blend toward token overlap.
            if llm == 0.0 and score > 0.5:
                score = min(score, 0.4)  # LLM says ungrounded; cap the optimistic overlap score
            elif llm == 1.0 and score < 0.5:
                score = max(score, 0.6)
    return float(score), True