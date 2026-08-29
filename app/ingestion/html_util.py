"""Minimal, dependency-free HTML -> readable text extraction for URL ingestion.

We deliberately avoid a heavy external dep (trafilatura/pdfminer) here so the hermetic test
suite stays network/dep-free; the goal is to strip markup and decode entities so that:
  * retrieved chunk text (and therefore citation snippets) are human-readable, not raw HTML, and
  * retrieval quality isn't polluted by tag soup.
For production-quality extraction a tenant can pre-clean; this is the safe default.
"""
from __future__ import annotations

import html
import re

_BLOCK_RE = re.compile(r"(?is)<(script|style)\b[^>]*>.*?</\1>")
_TAG_RE = re.compile(r"(?i)<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_MULTI_NL_RE = re.compile(r"\n{3,}")


def html_to_text(raw: str) -> str:
    """Strip HTML markup and return readable text.

    Removes <script>/<style> blocks entirely, drops all remaining tags, decodes HTML
    entities, then collapses runs of whitespace and blank lines so chunked text is clean.
    """
    cleaned = _BLOCK_RE.sub(" ", raw)
    cleaned = _TAG_RE.sub(" ", cleaned)
    cleaned = html.unescape(cleaned)
    cleaned = _WS_RE.sub(" ", cleaned)
    cleaned = _MULTI_NL_RE.sub("\n\n", cleaned)
    return cleaned.strip()
