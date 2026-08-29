"""PHASE F (#30-#34) — document parsing depth (heading/table/code fidelity + char_span).

Validates that chunk_structured() preserves heading hierarchy, marks table blocks, keeps code
fences whole, and attaches char_span citation offsets. Storage of this metadata into the vector
store is a separate (heavier) follow-up; here we test the chunker contract directly.
"""

from app.ingestion.chunker import chunk_structured, chunk_text, Chunk


def test_heading_hierarchy_inherited():
    md = "# Title\nIntro paragraph under title.\n\n## Sub\nDetail under sub.\n\nMore detail."
    chunks = chunk_structured(md, content_type="markdown")
    # At least one chunk carries the inherited heading context.
    headings = [c.metadata.get("heading") for c in chunks]
    assert "Title" in headings
    assert "Sub" in headings


def test_table_block_marked():
    md = (
        "# Report\n"
        "Here is the comparison:\n\n"
        "| Feature | A | B |\n"
        "| --- | --- | --- |\n"
        "| Speed | fast | slow |\n"
        "| Cost | low | high |\n\n"
        "End note."
    )
    chunks = chunk_structured(md, content_type="markdown")
    table_chunks = [c for c in chunks if c.metadata.get("is_table")]
    assert table_chunks, "no table chunk produced"
    tc = table_chunks[0]
    assert tc.metadata["kind"] == "table"
    assert tc.metadata["table_rows"] == 2  # two data rows
    assert "| Feature | A | B |" in tc.text


def test_code_fence_kept_whole():
    code = "```python\ndef add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a - b\n```"
    chunks = chunk_structured(code, content_type="code")
    # The whole fence is one chunk (not split mid-token).
    assert len(chunks) == 1
    assert chunks[0].metadata.get("kind") == "code"
    assert "def add" in chunks[0].text and "def sub" in chunks[0].text


def test_char_span_points_to_source():
    text = "Alpha line here.\n\nBeta line there.\n\nGamma line end."
    chunks = chunk_structured(text, content_type="text")
    # Reconstruct each chunk from the original by its char_span.
    for c in chunks:
        assert c.char_span is not None
        start, end = c.char_span
        assert text[start:end] == c.text


def test_markdown_char_span_alignment():
    md = "# H\nPara one.\n\nPara two."
    chunks = chunk_structured(md, content_type="markdown")
    for c in chunks:
        assert c.char_span is not None
        s, e = c.char_span
        assert md[s:e] == c.text


def test_plain_text_fallback_has_spans():
    chunks = chunk_text("Word " * 50, content_type="text")
    assert all(c.char_span is not None for c in chunks)
    # spans are non-overlapping-ish (each chunk maps to a real substring)
    for c in chunks:
        assert c.text in "Word " * 50
