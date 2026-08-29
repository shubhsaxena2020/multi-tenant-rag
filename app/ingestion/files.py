"""File upload parsing for ingestion (PHASE B.3).

Parse uploaded files into plain text ready for chunking:
  - PDF  -> pypdf (pure-python, zero native build). If pypdf is not installed we raise a
            clear, actionable error rather than silently dropping the file.
  - Markdown / HTML / code / text -> passed through (markdown is already a first-class
            content_type for the chunker; HTML goes through the existing html handling).

The extractor returns (text, content_type) so the caller can upsert without guessing.
"""
from __future__ import annotations

import io

from ..observability import get_logger

log = get_logger("ingestion")

# Map file extensions -> content_type understood by the ingestion pipeline.
_EXT_CONTENT_TYPE = {
    ".md": "markdown",
    ".markdown": "markdown",
    ".txt": "text",
    ".text": "text",
    ".html": "html",
    ".htm": "html",
    ".pdf": "pdf",
    ".py": "code",
    ".js": "code",
    ".ts": "code",
    ".tsx": "code",
    ".jsx": "code",
    ".go": "code",
    ".java": "code",
    ".rb": "code",
    ".rs": "code",
    ".c": "code",
    ".cpp": "code",
    ".h": "code",
    ".sh": "code",
    ".sql": "code",
    ".json": "code",
    ".yaml": "yaml",
    ".yml": "yaml",
}


def detect_content_type(filename: str, explicit: str | None = None) -> str:
    """Resolve the content_type for an upload, preferring an explicit value."""
    if explicit and explicit.lower() in {
        "text", "markdown", "html", "code", "pdf", "yaml",
    }:
        return explicit.lower()
    name = (filename or "").lower()
    for ext, ct in _EXT_CONTENT_TYPE.items():
        if name.endswith(ext):
            return ct
    return "text"


def extract_text(filename: str, data: bytes, content_type: str) -> str:
    """Return plain text for the given file + resolved content_type.

    For PDF we use pypdf (optional dep). For everything else we decode bytes as UTF-8
    (markdown/html/code/text/yaml are all text formats). Raises ValueError with an
    actionable message if a required parser (pypdf) is missing.
    """
    ct = content_type.lower()
    if ct == "pdf":
        return _extract_pdf(data)
    # All other supported types are text.
    try:
        return data.decode("utf-8", errors="replace")
    except Exception as e:  # pragma: no cover - decode almost never hard-fails
        raise ValueError(f"could not decode {filename or 'file'} as text: {e}") from e


def _extract_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as e:  # Operator must install the parser; surface a clear error.
        raise ValueError(
            "PDF parsing requires `pypdf` (not installed). Install with: "
            "uv pip install pypdf   — then retry the upload."
        ) from e
    reader = PdfReader(io.BytesIO(data))
    parts: list[str] = []
    for page in reader.pages:
        try:
            txt = page.extract_text() or ""
        except Exception as ex:
            log.warning("pdf_page_extract_failed", extra={"error_type": type(ex).__name__})
            txt = ""
        if txt.strip():
            parts.append(txt)
    text = "\n\n".join(parts).strip()
    if not text:
        raise ValueError("PDF contained no extractable text (scanned/image PDF?)")
    return text
