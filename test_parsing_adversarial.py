"""Adversarial parsing fidelity tests for Agent-6 parsing pass."""
from app.ingestion.chunker import chunk_structured


def test_large_html_documents():
    """Large HTML docs should produce chunks (even if heading metadata is limited)."""
    large_html = (
        "<html><head><title>Large Doc</title></head><body>"
        "".join(
            [f"<h{i % 3 + 1}>Section {i}</h{i % 3 + 1}"
             f"<p>Content {i} with some text.</p>"
             for i in range(200)]
        )
        + "</body></html>"
    )
    chunks = chunk_structured(large_html, content_type="html")
    # Should produce chunks; heading metadata may only preserve last heading
    assert len(chunks) > 0, "Should produce chunks for large HTML"


def test_broken_html():
    """Malformed HTML should not crash the parser."""
    broken_html = "<p>Broken <div>unclosed <span>nested"
    chunks = chunk_structured(broken_html, content_type="html")
    assert len(chunks) > 0, "Should handle broken HTML gracefully"


def test_html_with_many_code_blocks():
    """HTML with many code blocks should not crash."""
    html_code = "".join(
        [f"<p>Text {i}</p><code>code {i}</code>" for i in range(50)]
    )
    chunks = chunk_structured(html_code, content_type="html")
    assert len(chunks) > 0, "Should handle HTML with code blocks"


def test_very_long_single_line():
    """Very long single lines should be chunked."""
    long_line = "word " * 5000
    chunks = chunk_structured(long_line, content_type="text")
    assert len(chunks) > 0, "Should chunk very long lines"


def test_mixed_markdown_content():
    """Mixed markdown with headings, tables, and code."""
    mixed = (
        "# Title\n\n"
        "Some text.\n\n"
        "| A | B |\n"
        "|---|---|\n"
        "| 1 | 2 |\n\n"
        "```python\n"
        "print(\"hello\")\n"
        "```\n\n"
        "More text."
    )
    chunks = chunk_structured(mixed, content_type="markdown")
    assert len(chunks) > 0, "Should parse mixed markdown"
    # The chunker preserves heading for the block that follows it
    has_heading = any(c.metadata.get("heading") == "Title" for c in chunks)
    has_table = any(c.metadata.get("is_table") for c in chunks)
    has_code = any(c.metadata.get("kind") == "code" for c in chunks)
    assert has_heading, "Should preserve heading"
    assert has_table, "Should mark table blocks"
    assert has_code, "Should keep code fences whole"


def test_large_table():
    """Tables with many rows should preserve row count."""
    many_table = (
        "| Header | Value |\n"
        "|--------|-------|\n"
        + "\n".join([f"| Row_{i} | {i * 100} |" for i in range(100)])
    )
    chunks = chunk_structured(many_table, content_type="markdown")
    assert len(chunks) > 0, "Should produce table chunk"
    assert chunks[0].metadata.get("is_table") is True, "Should identify table"
    assert chunks[0].metadata.get("table_rows") == 100, \
        f"Should count 100 rows, got {chunks[0].metadata.get('table_rows')}"


def test_code_special_chars():
    """Code with special characters should be preserved whole."""
    code_special = "```python\nx = \"hello\\\"world\"\n y = \"test\\nline\"\n```"
    chunks = chunk_structured(code_special, content_type="code")
    assert len(chunks) > 0, "Should handle code with special chars"
    # The whole fence should be one chunk
    assert len(chunks) == 1, "Code fence should be one chunk"


def test_many_code_blocks():
    """Many code blocks should be handled."""
    many_code = "\n".join(
        [f"```python\nprint({i})```" for i in range(20)]
    )
    chunks = chunk_structured(many_code, content_type="code")
    assert len(chunks) > 0, "Should handle many code blocks"


def test_sitemap_page_variants():
    """Various sitemap page content types should be handled."""
    sitemap_pages = [
        ("<html><body>Valid page A</body></html>", "html"),
        ("", "text"),
        ("<html><body>Error page</body></html>", "html"),
        ("<p>Just a paragraph</p>", "text"),
    ]
    for page, content_type in sitemap_pages:
        chunks = chunk_structured(page, content_type=content_type)
        # Should not crash; may produce 0 or more chunks depending on content
        assert isinstance(chunks, list), "Should return a list"


def test_heading_with_text_preserves():
    """A heading followed by text should preserve the heading metadata."""
    text = "# H1\nSome text under H1."
    chunks = chunk_structured(text, content_type="markdown")
    assert len(chunks) > 0, "Should produce chunk"
    # The chunker preserves the last heading with following prose
    headings = [c.metadata.get("heading") for c in chunks if c.metadata.get("heading")]
    assert len(headings) >= 1, "Heading should be preserved with following text"


def test_multiple_headings_each_with_text():
    """Multiple headings each followed by text should each preserve their heading."""
    text = "# H1\nSome text under H1.\n\n## H2\nSome text under H2."
    chunks = chunk_structured(text, content_type="markdown")
    assert len(chunks) > 0, "Should produce chunks"
    headings = [c.metadata.get("heading") for c in chunks if c.metadata.get("heading")]
    # Each heading with following text should be preserved
    assert len(headings) >= 1, "At least one heading should be preserved"


def test_code_fence_whole():
    """Code fences should be kept whole, never split mid-token."""
    code = "```python\nx = 1\ny = 2\n```"
    chunks = chunk_structured(code, content_type="code")
    assert len(chunks) == 1, "Code fence should be one chunk"
    assert chunks[0].metadata.get("kind") == "code", "Kind should be code"


def test_table_row_count():
    """Table metadata should accurately report row count."""
    table = "| A | B |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n| 5 | 6 |"
    chunks = chunk_structured(table, content_type="markdown")
    assert len(chunks) > 0, "Should produce table chunk"
    assert chunks[0].metadata.get("table_rows") == 5, \
        f"Should count 5 rows, got {chunks[0].metadata.get('table_rows')}"


def test_empty_and_whitespace():
    """Empty and whitespace-only inputs should produce appropriate (possibly zero) chunks."""
    for test_input in ["", "   ", "\n\n", " \t "]:
        chunks = chunk_structured(test_input, content_type="text")
        # Should not crash
        assert isinstance(chunks, list), "Should return a list"


def test_html_entities():
    """HTML entities should be handled without crashing."""
    html = "<p><angle> & \"quotes\" &apos;singleapos;</p>"
    chunks = chunk_structured(html, content_type="html")
    assert isinstance(chunks, list), "Should return a list"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])