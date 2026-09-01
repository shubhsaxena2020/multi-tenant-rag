"""Regression tests for faithfulness _tokens and is_refusal heuristics."""

from app.faithfulness import _tokens, is_refusal, score_faithfulness


def test_refusal_detection():
    """Ensure refusal answers are detected and produce 0.0 faithfulness."""
    # "I don't know" should be detected as refusal
    assert is_refusal("I don't know") == True, "Should detect refusal in 'I don't know'"
    # "I do not know" should also be refusal
    assert is_refusal("I do not know") == True, "Should detect refusal in 'I do not know'"

    # Refusal answers must produce (0.0, False) — zero faithfulness, not answerable
    result = score_faithfulness("I don't know", ["some context"])
    assert result == (0.0, False), f"Refusal should give (0.0, False), got {result}"

    result = score_faithfulness("I do not know", ["some context"])
    assert result == (0.0, False), f"Refusal should give (0.0, False), got {result}"


def test_empty_answer_refusal():
    """Ensure empty answer is treated as refusal."""
    assert is_refusal("") == True, "Empty answer should be refusal"
    result = score_faithfulness("", ["some context"])
    assert result == (0.0, False), f"Empty answer should give (0.0, False), got {result}"


def test_normal_answer_not_refusal():
    """Ensure normal answers are not refusal and have positive faithfulness."""
    assert is_refusal("The answer is correct") == False, "Normal answer should not be refusal"
    score, answerable = score_faithfulness("The answer", ["context has answer"])
    assert answerable == True, "Normal answer should be answerable"
    assert score > 0, "Normal answer should have positive faithfulness score"


def test_token_normalization():
    """Ensure _tokens works correctly across edge cases."""
    # Basic tokenization
    tokens = _tokens("The capital of France is Paris.")
    assert isinstance(tokens, list), "_tokens should return a list"
    assert len(tokens) > 0, "Should tokenize a normal sentence"

    # Empty input
    tokens_empty = _tokens("")
    assert tokens_empty == [], "Empty input should yield empty token list"

    # Only stopwords
    tokens_sw = _tokens("the a an")
    assert tokens_sw == [], "Stopword-only input should yield empty token list"