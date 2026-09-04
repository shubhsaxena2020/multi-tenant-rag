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
    "sorry",
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
    # Normalize: strip apostrophes from contractions like "don't" -> "dont"
    norm = low.replace("'", "")
    markers = (
        "i dont know", "i do not know", "i cannot", "i cant",
        "no information", "dont have information", "do not have information",
        "imsorry", "isorry", "unable to", "cant help", "cannot help",
        "sorry",
    )
    return any(m in norm for m in markers)


def _detect_negation_contradiction(answer: str, context: str) -> bool:
    """Check if the answer contains negation that contradicts the context.

    Detects patterns like "X is NOT in Y" when context says "X is in Y", or any answer token with 'not'/'n't' that negates a claim present in context.
    Also detects "did not [verb]" patterns where the context affirms the same verb (e.g., "did not sink" vs "sank").
    """
    ans_lower = answer.lower()
    ctx_lower = context.lower()

    # Find negated phrases in answer (e.g., "not X", "isn't X", "don't X")
    negated_terms = re.findall(r"([a-z0-9]+\s+not|n't)", ans_lower)

    for term in negated_terms:
        term_clean = term.replace("n't", "").strip()
        # If the negated term appears in context, it's a contradiction
        if term_clean and term_clean in ctx_lower:
            return True

    # Detect "did not [verb]" / "didn't [verb]" patterns where
    # the context affirms the same verb (e.g., "did not sink" vs "sank")
    neg_verb_patterns = re.findall(r"did\s+not\s+([a-z0-9]+)|didn't\s+([a-z0-9]+)", ans_lower)
    for match in neg_verb_patterns:
        neg_verb = match[0] if match[0] else match[1]
        neg_verb_lower = neg_verb.lower()
        # Check if the same verb appears positively in context
        # Handle both base form and various past tense forms
        # Include common irregular past forms
        verb_variants = [
            neg_verb_lower,                       # base form (sit)
            neg_verb_lower + "ed",                 # regular past (sited - rare)
            "sat",                                 # irregular past of sit
            neg_verb_lower + "s",                  # sinks (3rd person)
            "sank",                                # sank (common past of sink)
        ]
        for verb_var in verb_variants:
            if verb_var in ctx_lower:
                return True

    # Check for "is not", "are not", "was not", "were not" patterns
    # where the negated phrase's subject/claim appears in context
    for pattern in ["is not", "are not", "was not", "were not"]:
        # Extract the phrase after "not" and check if it appears in context
        if pattern in ans_lower:
            # Get text after the pattern
            after_not = ans_lower.split(pattern, 1)[1]
            # Take the first word/phrase after "not" as the negated claim
            claim_words = after_not.strip().split()[:3]
            claim = " ".join(claim_words)
            if claim and claim in ctx_lower:
                return True

    # Also check for "isn't", "aren't", "wasn't", "weren't" contractions
    for contraction in ["isn't", "is not", "aren't", "are not", "wasn't", "was not", "weren't", "were not"]:
        if contraction.replace("n't", " is") in ans_lower or contraction in ans_lower:
            # Extract claim after contraction
            clean = contraction.replace("n't", " is")
            if clean in ans_lower:
                after = ans_lower.split(clean, 1)[1]
                claim_words = after.strip().split()[:3]
                claim = " ".join(claim_words)
                if claim and claim in ctx_lower:
                    return True

    return False


def _has_entity_substitution(answer: str, context: str) -> bool:
    """Detect when the answer contains key entities that are substituted/inverted relative to context.

    This catches cases like "spaceship" vs "passenger ship" for the Titanic, or
    any answer that claims a drastically different entity type while sharing some
    surface tokens. When >30% of answer tokens are absent from context AND the absent
    tokens include domain-specific nouns that represent a core entity shift, the
    grounding is unreliable.

    We only flag this when ALL of the following hold:
    1. More than 30% of answer tokens are absent from context, AND
    2. The absent tokens include at least one substantive noun (not a stopword,
       functional word, or short adjective - typically 3+ chars, alphabetic, and
       not a common question word like when/why/how), AND
    3. At least half the answer tokens are absent with some overlap present,
       indicating the answer is making different core claims about entities
    """
    ans_tokens = set(_tokens(answer))
    ctx_tokens = set(_tokens(context))

    if not ans_tokens or not ctx_tokens:
        return False

    absent_tokens = ans_tokens - ctx_tokens
    overlap_tokens = ans_tokens & ctx_tokens

    # If more than 30% of answer tokens are absent from context...
    if len(absent_tokens) / len(ans_tokens) <= 0.3:
        return False

    # Functional/question words that should NOT trigger entity substitution
    # even when absent from context: when, how, why, did, do, does, is, are, etc.
    functional_words = {
        "when", "how", "why", "did", "do", "does", "did", "is", "are", "was", "were",
        "has", "have", "had", "will", "would", "should", "could", "may", "might",
        "the", "a", "an", "and", "or", "but", "so", "if", "because", "than",
        "not", "no", "none", "some", "any", "each", "every", "both", "all",
        "other", "such", "only", "own", "same", "so", "too", "very",
    }
    # Substantive absent tokens: alphabetic, 3+ chars, NOT a functional word
    domain_absent = {
        t for t in absent_tokens
        if len(t) >= 3 and t.isalpha() and t not in functional_words
    }

    if not domain_absent:
        # All absent tokens are just functional words — not an entity substitution
        return False

    # If at least half the answer tokens are domain-specific absent and there's overlap,
    # it's a major grounding gap
    if len(domain_absent) / len(ans_tokens) >= 0.5 and len(overlap_tokens) > 0:
        return True

    return False


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
                    f"CONTEXT:{context[:2000]}\n\nANSWER:{answer[:1000]}"
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
    - When token overlap score is 0.0 (zero grounding), answer is not answerable
      since it has no support in the retrieved context.
    - Tightened heuristic: if the answer contains tokens not present in the retrieved context
      (beyond stopword-level overlap), the faithfulness score is proportionally reduced so that
      answers whose content is mostly unsupported by context receive a lower faithfulness score
      and are less likely to be marked answerable.
    - Contradiction hardening: if the answer contains claims that fundamentally contradict
      the retrieved context (e.g., entity substitutions, factual inversions), the score is
      zeroed and the answer is marked unanswerable, since the grounding is unreliable.
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
    # Tightened heuristic: zero token overlap means zero grounding -> not answerable
    if score == 0.0:
        return 0.0, False
    # NEW: penalize score when answer has tokens absent from context.
    # This prevents answers with high surface-overlap but wrong factual claims
    # from receiving a deceptively high faithfulness score.
    ans_tokens = _tokens(answer or "")
    ctx_tokens = set(_tokens(context))
    if ans_tokens:
        absent_tokens = [t for t in ans_tokens if t not in ctx_tokens]
        absence_ratio = len(absent_tokens) / len(ans_tokens)
        if absence_ratio > 0.3 and score > 0.3:
            # More than 30% of answer tokens are absent from context, and we had
            # non-trivial overlap — cap the score to reflect the grounding gap.
            score = score * (1 - absence_ratio * 0.6)
    # Contradiction hardening: if answer has entity substitutions or fundamental
    # factual contradictions relative to context, zero the score.
    context_str = " ".join(context_chunks) if context_chunks else ""
    if _has_entity_substitution(answer or "", context_str):
        return 0.0, False
    # Tightened answerability: if majority of answer tokens are absent from context,
    # the answer is not reliably answerable even if some tokens overlap.
    if score > 0:
        absent_count = len([t for t in _tokens(answer or "") if t not in ctx_tokens])
        total_answer_tokens = len(_tokens(answer or ""))
        if total_answer_tokens > 0 and absent_count / total_answer_tokens > 0.5:
            return 0.0, False
    if _detect_negation_contradiction(answer or "", context):
        return 0.0, False
    return float(score), True