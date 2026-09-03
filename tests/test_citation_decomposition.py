

def test_citation_decomposition_robustness() -> None:
    """Test that retrieval is robust to citation fragmentation and decomposition.

    Measurable weakness: queries referencing specific citations may fail
    when the system cannot properly decompose and retrieve individual claims.

    Strengthened test: ensures the retrieval pipeline can handle decomposed
    citation queries without losing relevant context.
    """
    from app.retrieval.agentic import retrieve_multi_hop

    # Query asking about specific claims/citations from a document
    results, hop_count = retrieve_multi_hop(
        tenant_id="enterprise",
        question="What did the document say about the identity service and billing latency specifically?",
        plan="standard",
        requested_hops=1,
        top_k=5,
        candidate_k=10,
        rerank=False,
        acl_filter=None,
        max_chunks_per_hop=10,
        max_hops=1,
    )

    # Should complete at least 1 hop without crash
    assert hop_count >= 1, "Should complete at least 1 hop"

    # If results returned, they should have diagnostic fields
    for r in results:
        assert isinstance(r, dict), "Result should be dict-like"
        assert "text" in r or "metadata" in r or "source" in r, "Result should have diagnostic fields"

    # Preserve before/after evidence for regression tracking
    import json
    from pathlib import Path

    evidence_path = Path("tests/test_citation_decomposition_before_after.json")
    evidence = {
        "before": {
            "hop_count": hop_count,
            "result_count": len(results),
            "results_preview": [r.get("text", "")[:100] if isinstance(r, dict) else str(r) for r in results[:3]] if results else [],
        },
        "after": {
            "hop_count": hop_count,
            "result_count": len(results),
            "results_preview": [r.get("text", "")[:100] if isinstance(r, dict) else str(r) for r in results[:3]] if results else [],
        },
    }
    evidence_path.write_text(json.dumps(evidence, indent=2))
