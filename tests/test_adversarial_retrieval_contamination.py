"""Additional adversarial retrieval contamination tests — prompt-poisoning and source-selection diagnostics."""

from app.retrieval.agentic import retrieve_multi_hop
from app.faithfulness import score_faithfulness, is_refusal


def test_prompt_poisoning_noise_flood():
    """Test that retrieval is robust to extreme prompt-poisoning noise."""
    # Extreme noise flood designed to confuse retrieval
    results, hop_count = retrieve_multi_hop(
        tenant_id="enterprise",
        question=" ".join(
            ["noise" + str(i) for i in range(50)]
            + ["what is the capital of France"]
            + ["noise" + str(i) for i in range(50)]
        ),
        plan=[],
        requested_hops=1,
        top_k=3,
        candidate_k=10,
        rerank=False,
        acl_filter=None,
        max_chunks_per_hop=10,
        max_hops=1,
    )
    # Should not crash and should complete 1 hop
    assert hop_count >= 1, "Should complete 1 hop despite extreme prompt noise"
    # If results returned, they should have diagnostics
    if results:
        for r in results:
            assert "metadata" in r or "source" in r, "Result should have diagnostics"


def test_source_selection_diagnostics_under_adversarial_prompts():
    """Test that source selection diagnostics are available under adversarial prompts."""
    results, hop_count = retrieve_multi_hop(
        tenant_id="enterprise",
        question="What is the effect of quantum computing on climate change?",
        plan="standard",
        requested_hops=1,
        top_k=3,
        candidate_k=5,
        rerank=False,
        acl_filter=None,
        max_chunks_per_hop=10,
        max_hops=1,
    )
    # Should complete 1 hop without crashing
    assert hop_count >= 1, "Should complete 1 hop"
    # Results should have source/metadata diagnostics when present
    if results:
        for r in results:
            assert "metadata" in r or "source" in r, "Result should have source/metadata diagnostics when present"


def test_refusal_detection_under_no_answer_turns():
    """Test that is_refusal correctly detects refusal patterns in no-answer turns."""
    # Standard refusal patterns
    assert is_refusal("I don't know") == True
    assert is_refusal("I do not know") == True
    assert is_refusal("I cannot answer") == True
    assert is_refusal("") == True  # empty answer is refusal

    # Non-refusals
    assert is_refusal("The answer is yes") == False
    assert is_refusal("The capital of France is Paris") == False
    assert is_refusal("Quantum physics explains everything") == False


def test_score_faithfulness_zero_overlap_no_context():
    """Test that faithfullness score is 0.0 with zero token overlap and no context."""
    # Zero token overlap with empty context should give (0.0, False)
    # per the tightened heuristic
    score, answerable = score_faithfulness(
        "The absolutely verified truth about quantum gravity is X",
        [],  # no context
    )
    # Score should reflect zero overlap
    assert score == 0.0, f"Expected 0.0 faithfulness with zero token overlap and no context, got {score}"
    # Tightened heuristic: zero grounding with no context -> not answerable
    assert answerable is False, f"Expected answerable=False with zero grounding and no context, got {answerable}"


def test_score_faithfulness_non_trivial_overlap():
    """Test that faithfullness score is > 0.0 with non-trivial token overlap."""
    score, answerable = score_faithfulness(
        "Paris is the capital of France",
        ["France is a country in Europe", "Paris is known for its architecture"],
    )
    # Should have some positive overlap
    assert score > 0.0, f"Expected > 0.0 faithfulness with token overlap, got {score}"
    # Should be answerable since there is grounding
    assert answerable is True, f"Expected answerable=True with positive grounding, got {answerable}"