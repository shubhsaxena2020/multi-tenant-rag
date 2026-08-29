"""File upload parsing for client onboarding (PHASE B.3).

A tenant uploads a file (PDF / Markdown / HTML / code / text) and we extract plain
text, then feed it through the normal ingestion pipeline (chunk -> embed -> store).
This complements URL scraping: same index, same isolation, same idempotent dedup
(content-hash for re-uploads without a doc_id, v10.10).

PDF parsing uses `pypdf` when installed. If the parser is unavailable we raise a
clear, actionable ValueError (surfaced by the endpoint as HTTP 400) instead of a 500.
"""
from __future__ import annotations

import io
import mimetypes
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

# Allowed values mirror app.validation.ALLOWED_CONTENT_TYPES.
_EXT_MAP = {
    ".md": "markdown",
    ".markdown": "markdown",
    ".txt": "text",
    ".text": "text",
    ".html": "html",
    ".htm": "html",
    ".xhtml": "html",
    ".pdf": "pdf",
    ".docx": "docx",
    ".py": "code",
    ".js": "code",
    ".ts": "code",
    ".java": "code",
    ".go": "code",
    ".rs": "code",
    ".c": "code",
    ".cpp": "code",
    ".h": "code",
    ".cs": "code",
    ".rb": "code",
    ".php": "code",
    ".sh": "code",
    ".sql": "code",
    ".json": "code",
    ".yaml": "code",
    ".yml": "code",
    ".toml": "code",
    ".xml": "code",
}

_MIME_MAP = {
    "text/markdown": "markdown",
    "text/x-markdown": "markdown",
    "text/plain": "text",
    "text/html": "html",
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
}

# Content types that require binary->text extraction (the rest pass through as-is).
_BINARY_TYPES = {"pdf", "docx"}


def detect_content_type(filename: str | None, content_type: str | None) -> str:
    """Resolve the pipeline content_type from the upload.

    Precedence: explicit content_type (if it names a known kind) -> file extension ->
    supplied MIME guess -> default 'text'. Returns one of the ALLOWED_CONTENT_TYPES
    except for 'pdf'/'docx' which are internal routing types resolved to a pipeline type
    (html/markdown/text) only AFTER extraction.
    """
    if content_type:
        ct = content_type.lower().split(";")[0].strip()
        # A caller may pass a pipeline type directly.
        if ct in {"markdown", "text", "html", "code"}:
            return ct
        if ct in _MIME_MAP:
            return _MIME_MAP[ct]
    ext = ""
    if filename and "." in filename:
        ext = "." + filename.rpartition(".")[2].lower()
        if ext in _EXT_MAP:
            return _EXT_MAP[ext]
    # Last resort: guess from the supplied MIME string.
    if content_type:
        guessed, _ = mimetypes.guess_type("x" + ext)
        if guessed and guessed in _MIME_MAP:
            return _MIME_MAP[guessed]
    return "text"


def extract_text(filename: str, data: bytes, ctype: str) -> str:
    """Extract plain text from an uploaded file.

    Raises ValueError with an actionable message for unsupported/missing parsers
    (surfaced as HTTP 400 by the caller). PDFs use pypdf when available.
    """
    if ctype == "pdf":
        return _extract_pdf(data)
    if ctype == "docx":
        return _extract_docx(data)
    # text / markdown / html / code all arrive as bytes; decode and lightly normalize.
    try:
        raw = data.decode("utf-8", errors="replace")
    except Exception as e:
        raise ValueError(f"could not decode file as UTF-8 text: {e}") from e
    if ctype == "html":
        return _strip_html(raw)
    # markdown / code / text: keep as-is (chunker handles whitespace). Preserve newlines.
    return raw


def _extract_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as e:  # pragma: no cover - exercised by the missing-parser test
        raise ValueError(
            "PDF parsing requires the 'pypdf' package; install it (pip install pypdf) "
            "or upload the document as text/markdown."
        ) from e
    try:
        reader = PdfReader(io.BytesIO(data))
        pages: list[str] = []
        for page in reader.pages:
            try:
                pages.append(page.extract_text() or "")
            except Exception:
                # A single unreadable page must not abort the whole document.
                pages.append("")
        text = "\n".join(pages).strip()
    except Exception as e:
        raise ValueError(f"failed to parse PDF: {type(e).__name__}: {e}") from e
    if not text:
        raise ValueError("PDF contained no extractable text (scanned/image PDF?)")
    return text


def _extract_docx(data: bytes) -> str:
    try:
        from docx import Document
    except ImportError as e:  # pragma: no cover
        raise ValueError(
            "DOCX parsing requires the 'python-docx' package; install it "
            "(pip install python-docx) or upload the document as text/markdown."
        ) from e
    try:
        doc = Document(io.BytesIO(data))
        parts: list[str] = [p.text for p in doc.paragraphs if p.text]
        # Tables: pull cell text so it isn't silently dropped.
        for table in doc.tables:
            for row in table.rows:
                for cell in row.cells:
                    if cell.text:
                        parts.append(cell.text)
        return "\n".join(parts).strip()
    except Exception as e:
        raise ValueError(f"failed to parse DOCX: {type(e).__name__}: {e}") from e


def _strip_html(html: str) -> str:
    """Best-effort HTML->text when trafilatura isn't installed (mirrors ssrf fallback)."""
    try:
        from html.parser import HTMLParser

        class _Text(HTMLParser):
            def __init__(self) -> None:
                super().__init__()
                self.out: list[str] = []

            def handle_data(self, data: str) -> None:
                self.out.append(data)

        p = _Text()
        p.feed(html)
        text = " ".join(p.out)
    except Exception:
        # Regex fallback: drop tags.
        import re

        text = re.sub(r"<[^>]+>", " ", html)
    # Collapse whitespace.
    import re

    return re.sub(r"\s+", " ", text).strip()
