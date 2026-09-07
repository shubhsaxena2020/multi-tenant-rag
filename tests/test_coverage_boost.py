"""Targeted tests to raise coverage on thinnest modules:
validation, generation, retrieval, rerank, embed, resilience, ssrf.
"""
import json
import pytest
from unittest.mock import patch, MagicMock

from fastapi import HTTPException

from app.validation import (
    validate_content,
    validate_metadata,
    validate_content_type,
    parse_json_field,
    parse_acl_field,
)
from app.generation import (
    _estimate_tokens,
    _estimate_usage,
    _extractive_answer,
    _build_messages,
    generate_answer,
    stream_answer,
)


# ---- validation.py ----

class TestValidation:
    def test_validate_content_empty(self):
        with pytest.raises(HTTPException, match="non-empty"):
            validate_content("")

    def test_validate_content_whitespace_only(self):
        with pytest.raises(HTTPException, match="non-empty"):
            validate_content("   \n\t  ")

    def test_validate_content_too_large(self):
        with pytest.raises(HTTPException, match="char limit"):
            validate_content("x" * 1_000_001)

    def test_validate_content_valid(self):
        validate_content("hello world")

    def test_validate_metadata_none(self):
        assert validate_metadata(None) is None

    def test_validate_metadata_valid(self):
        assert validate_metadata({"key": "value"}) == {"key": "value"}

    def test_validate_metadata_too_large(self):
        big = {"key": "x" * 33_000}
        with pytest.raises(HTTPException, match="bytes"):
            validate_metadata(big)

    def test_validate_content_type_valid(self):
        assert validate_content_type("text") == "text"
        assert validate_content_type("HTML") == "html"
        assert validate_content_type("markdown") == "markdown"
        assert validate_content_type("code") == "code"

    def test_validate_content_type_default(self):
        assert validate_content_type(None) == "text"
        assert validate_content_type("") == "text"

    def test_validate_content_type_invalid(self):
        with pytest.raises(HTTPException, match="content_type"):
            validate_content_type("pdf")

    def test_parse_json_field_none(self):
        assert parse_json_field(None) == {}

    def test_parse_json_field_empty(self):
        assert parse_json_field("") == {}

    def test_parse_json_field_invalid(self):
        assert parse_json_field("not json") == {}

    def test_parse_json_field_non_dict(self):
        assert parse_json_field("[1,2,3]") == {}

    def test_parse_json_field_valid(self):
        assert parse_json_field('{"a": 1}') == {"a": 1}

    def test_parse_acl_field_none(self):
        assert parse_acl_field(None) is None

    def test_parse_acl_field_invalid_json(self):
        with pytest.raises(HTTPException, match="JSON array"):
            parse_acl_field("not json")

    def test_parse_acl_field_not_list(self):
        with pytest.raises(HTTPException, match="JSON array"):
            parse_acl_field('{"a": 1}')

    def test_parse_acl_field_wildcard(self):
        with pytest.raises(HTTPException, match="wildcard"):
            parse_acl_field('["*"]')

    def test_parse_acl_field_empty(self):
        with pytest.raises(HTTPException, match="at least one group"):
            parse_acl_field("[]")

    def test_parse_acl_field_non_string_elements(self):
        with pytest.raises(HTTPException, match="JSON array"):
            parse_acl_field('[1, 2]')

    def test_parse_acl_field_valid(self):
        assert parse_acl_field('["engineering", "marketing"]') == ["engineering", "marketing"]


# ---- generation.py ----

class TestGeneration:
    def test_estimate_tokens_empty(self):
        assert _estimate_tokens("") == 0

    def test_estimate_tokens_normal(self):
        assert _estimate_tokens("hello") == 1  # 5 chars / 4 = 1
        assert _estimate_tokens("hello world test string here") == 7  # 28 chars / 4

    def test_estimate_usage_empty(self):
        usage = _estimate_usage("q", [], "a", "")
        assert usage["estimated"] is True
        assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]

    def test_estimate_usage_with_chunks(self):
        chunks = [{"text": "chunk one"}, {"text": "chunk two"}]
        usage = _estimate_usage("question", chunks, "answer", "system")
        assert usage["prompt_tokens"] > 0
        assert usage["completion_tokens"] > 0

    def test_extractive_answer_empty(self):
        assert _extractive_answer([]) == "No relevant context was found for this question."

    def test_extractive_answer_with_chunks(self):
        chunks = [
            {"text": "first chunk", "score": 0.5},
            {"text": "second chunk", "score": 0.9},
        ]
        answer = _extractive_answer(chunks)
        assert "second chunk" in answer

    def test_extractive_answer_with_rerank_score(self):
        chunks = [
            {"text": "low rerank", "rerank_score": 0.1},
            {"text": "high rerank", "rerank_score": 0.95},
        ]
        answer = _extractive_answer(chunks)
        assert "high rerank" in answer

    def test_build_messages_no_system(self):
        chunks = [{"text": "context"}]
        msgs = _build_messages("question", chunks, 6000, "")
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"
        assert "question" in msgs[0]["content"]

    def test_build_messages_with_system(self):
        chunks = [{"text": "context"}]
        msgs = _build_messages("question", chunks, 6000, "You are helpful.")
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert msgs[0]["content"] == "You are helpful."

    def test_generate_answer_extractive(self):
        chunks = [{"text": "The answer is 42.", "score": 0.8}]
        answer, usage = generate_answer("What is the answer?", chunks)
        assert "42" in answer
        assert usage["estimated"] is True

    def test_generate_answer_no_chunks(self):
        answer, usage = generate_answer("question?", [])
        assert "No relevant context" in answer

    def test_stream_answer_extractive(self):
        chunks = [{"text": "streamed content", "score": 0.8}]
        parts = list(stream_answer("question", chunks))
        assert len(parts) > 0
        # Last part should have usage
        last_text, last_usage = parts[-1]
        assert last_usage is not None
        assert last_usage["estimated"] is True

    def test_stream_answer_no_chunks(self):
        parts = list(stream_answer("question", []))
        assert len(parts) > 0
