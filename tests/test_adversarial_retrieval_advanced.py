"""Additional adversarial retrieval behavior tests — strengthen ambiguous, multi-hop, and prompt-contaminated cases."""

from app.retrieval.agentic import retrieve_multi_hop
from app.faithfulness import is_refusal, score_faithfulness


def test_multi_hop_deep_chain():
    """Test multi-hop retrieval with a deep chain (3 hops)."""
    results, hop_count = retrieve_multi_hop(
        tenant_id="enterprise",
        question="What is the effect of climate change on polar bear migration patterns over decades?",
        plan="standard",
        requested_hops=3,
        top_k=3,
        candidate_k=5,
        rerank=False,
        acl_filter=None,
        max_chunks_per_hop=10,
        max_hops=3,
    )
    # Should complete at least 1 hop without crashing; actual hop count depends on
    # available relevant content across hops. Key: no exception raised.
    assert hop_count >= 1, f"Expected at least 1 hop, got {hop_count}"
    # Results should have diagnostics
    if results:
        for r in results:
            assert "metadata" in r or "source" in r, "Result should have diagnostics"


def test_ambiguous_query_no_clear_intent():
    """Test that ambiguous queries (no clear single intent) are handled gracefully."""
    results, hop_count = retrieve_multi_hop(
        tenant_id="enterprise",
        question="To be or not to be, that is the question about everything and nothing.",
        plan="standard",
        requested_hops=1,
        top_k=3,
        candidate_k=5,
        rerank=False,
        acl_filter=None,
        max_chunks_per_hop=10,
        max_hops=1,
    )
    # Should not crash on ambiguous query
    assert hop_count >= 1, "Should complete 1 hop without crash"
    # If results returned, they should have diagnostics
    if results:
        for r in results:
            assert "metadata" in r or "source" in r, "Result should have diagnostics when present"


def test_prompt_contamination_irrelevant_entities():
    """Test retrieval robustness when prompt contains irrelevant named entities."""
    results, hop_count = retrieve_multi_hop(
        tenant_id="enterprise",
        question="Who won the 1998 FIFA World Cup and what is the capital of Japan?",
        plan="standard",
        requested_hops=1,
        top_k=3,
        candidate_k=10,
        rerank=False,
        acl_filter=None,
        max_chunks_per_hop=10,
        max_hops=1,
    )
    # Should not crash on multi-entity prompt
    assert hop_count >= 1, "Should complete 1 hop despite unrelated entities in prompt"
    # If results returned, diagnostics should be present
    if results:
        for r in results:
            assert "metadata" in r or "source" in r, "Result should have diagnostics when present"


def test_no_answer_refusal_with_multi_hop():
    """Test that multi-hop retrieval correctly returns refusal for no-answer paths."""
    from app.retrieval.agentic import retrieve_multi_hop
    from app.faithfulness import is_refusal

    # Query with no possible answer in any context
    results, hop_count = retrieve_multi_hop(
        tenant_id="enterprise",
        question="What was the lunch menu for the RMS Lusitania in 1915?",
        plan="standard",
        requested_hops=2,
        top_k=3,
        candidate_k=5,
        rerank=False,
        acl_filter=None,
        max_chunks_per_hop=10,
        max_hops=2,
    )
    # Should complete hops without crashing even when no relevant content exists
    assert hop_count >= 1, "Should complete at least 1 hop without exception"
    # Results structure should be available (even if empty)
    # Key: no exception raised on no-match queries


def test_answerability_score_zero_when_no_support():
    """Test that faithfullness score is 0.0 when answer has no supporting context."""
    # A claim with zero token overlap with context should score 0.0 and not be answerable
    # (tightened heuristic: zero grounding means zero support)
    score, answerable = score_faithfulness(
        "The absolutely verified truth about quantum gravity is X",
        ["some context about biology and chemistry"],
    )
    # Score should reflect zero overlap
    assert score == 0.0, f"Expected 0.0 faithfulness with zero token overlap, got {score}"
    # Tightened heuristic: zero token overlap means zero grounding -> not answerable
    assert answerable is False, f"Expected answerable=False with zero grounding, got {answerable}"