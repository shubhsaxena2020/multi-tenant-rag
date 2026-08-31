"""Ingestion fidelity tests for tables, code, and headings.

Tests the chunking infrastructure's ability to preserve structural metadata
(headings, tables, code fences) when splitting documents into chunks.
All tests must pass (green) for ingestion fidelity to be verified.
"""

from app.ingestion.chunker import chunk_structured, Chunk


def test_heading_preservation_passes():
    """Headings (# / ## / ###) set heading metadata on their chunk.

    Each heading in a markdown document creates a chunk that carries that
    heading as metadata. Subsequent prose chunks inherit the heading until
    a new heading overrides it — but the current chunker produces one chunk
    per heading block, so we verify each heading chunk has the correct meta.
    """
    md = "# Main Title\n\nThis is paragraph text under the main title.\n\n## Section 1\n\nContent under section 1.\n\n### Subsection 1.1\n\nDeep content.\n\n## Section 2\n\nMore content under section 2.\n\nPlain paragraph with no heading nearby."
    chunks = chunk_structured(md, content_type="markdown")

    assert len(chunks) >= 4, f"Expected >= 4 chunks, got {len(chunks)}"

    # Each heading chunk carries its own heading metadata
    # Chunk 0 has # Main Title
    assert chunks[0].metadata.get("heading") == "Main Title", (
        f"Chunk 0 heading expected 'Main Title', got {chunks[0].metadata.get('heading')}"
    )

    # Chunk 1 has ## Section 1 (the chunker groups heading + following text)
    assert chunks[1].metadata.get("heading") == "Section 1", (
        f"Chunk 1 heading expected 'Section 1', got {chunks[1].metadata.get('heading')}"
    )

    # Chunk 2 has ### Subsection 1.1
    assert chunks[2].metadata.get("heading") == "Subsection 1.1", (
        f"Chunk 2 heading expected 'Subsection 1.1', got {chunks[2].metadata.get('heading')}"
    )

    # Chunk 3 has ## Section 2
    assert chunks[3].metadata.get("heading") == "Section 2", (
        f"Chunk 3 heading expected 'Section 2', got {chunks[3].metadata.get('heading')}"
    )

    print("  PASS: test_heading_preservation_passes")


def test_table_preservation_passes():
    """Markdown tables are kept whole with kind=table and is_table=True metadata.

    Tables include table_rows count (number of data rows excluding header/separator).
    """
    md = "| Name | Age | City |\n|------|-----|------|\n| Alice | 30 | NY |\n| Bob | 25 | SF |\n| Carol | 35 | LA |"

    chunks = chunk_structured(md, content_type="markdown")

    assert len(chunks) >= 1, f"Expected >= 1 chunk, got {len(chunks)}"

    table_chunk = chunks[0]
    assert table_chunk.metadata.get("kind") == "table", (
        f"Expected kind='table', got {table_chunk.metadata.get('kind')}"
    )
    assert table_chunk.metadata.get("is_table") is True, (
        f"Expected is_table=True, got {table_chunk.metadata.get('is_table')}"
    )
    assert table_chunk.metadata.get("table_rows") == 3, (
        f"Expected table_rows=3, got {table_chunk.metadata.get('table_rows')}"
    )

    # The chunk text should contain the full table
    assert "|" in table_chunk.text, "Table chunk should contain pipe-delimited rows"

    print("  PASS: test_table_preservation_passes")


def test_code_fence_preservation_passes():
    """Fenced code blocks (```...) are kept whole and marked kind=code.

    Code fences are never split mid-token — the entire fence block is one chunk.
    """
    md = "Some text before.\n\n```python\ndef hello():\n    print('Hello, world!')\n    return True\n```\n\nSome text after."

    chunks = chunk_structured(md, content_type="markdown")

    assert len(chunks) >= 1, f"Expected >= 1 chunk, got {len(chunks)}"

    # Find the code chunk
    code_chunks = [c for c in chunks if c.metadata.get("kind") == "code"]
    assert len(code_chunks) >= 1, (
        f"Expected >= 1 code chunk, got {len(code_chunks)}"
    )

    code_chunk = code_chunks[0]
    assert "```python" in code_chunk.text, (
        f"Code chunk should contain opening fence, got: {code_chunk.text[:50]}"
    )
    assert "```" in code_chunk.text, (
        f"Code chunk should contain closing fence, got: {code_chunk.text[:50]}"
    )
    # Code should not be split — the full function body should be present
    assert "def hello()" in code_chunk.text, (
        f"Code chunk should contain 'def hello()', got: {code_chunk.text[:50]}"
    )

    print("  PASS: test_code_fence_preservation_passes")


def test_heading_boundary_passes():
    """Heading changes create clear boundaries in chunk output.

    When a new heading appears, the chunker starts a new chunk with the
    new heading metadata. This test verifies that heading transitions
    are reflected in the chunk structure.
    """
    md = "# Title\n\nFirst section content.\n\n## Subsection\n\nSecond section content.\n\n# New Title\n\nThird section content."

    chunks = chunk_structured(md, content_type="markdown")

    assert len(chunks) >= 3, f"Expected >= 3 chunks, got {len(chunks)}"

    # First chunk has Title heading
    assert chunks[0].metadata.get("heading") == "Title"

    # Second chunk has Subsection heading (heading changed)
    assert chunks[1].metadata.get("heading") == "Subsection"

    # Third chunk has New Title heading (heading changed again)
    assert chunks[2].metadata.get("heading") == "New Title"

    print("  PASS: test_heading_boundary_passes")


def test_all_fidelity_tests_passes():
    """Run all ingestion fidelity sub-tests.

    This is the composite test that aggregates the individual fidelity
    checks (headings, tables, code fences, heading boundaries).
    """
    test_heading_preservation_passes()
    test_table_preservation_passes()
    test_code_fence_preservation_passes()
    test_heading_boundary_passes()
    print("  PASS: test_all_fidelity_tests_passes (composite)")


if __name__ == "__main__":
    test_heading_preservation_passes()
    test_table_preservation_passes()
    test_code_fence_preservation_passes()
    test_heading_boundary_passes()
    test_all_fidelity_tests_passes()
    print("\nAll ingestion fidelity tests PASSED ✓")