"""Validation constants and helpers for the public API surface."""
from __future__ import annotations

from fastapi import HTTPException

# Hard caps to keep ingestion bounded and prevent abuse / OOM on the VPS.
MAX_CONTENT_CHARS = 1_000_000          # ~1M chars (~4MB text) per document
MAX_URL_LENGTH = 8_000
MAX_METADATA_BYTES = 32_000            # serialized JSON size cap
MAX_TITLE_CHARS = 300
ALLOWED_CONTENT_TYPES = {"text", "markdown", "html", "code"}


def _fail(msg: str) -> None:
    raise HTTPException(status_code=422, detail=msg)


def validate_content(content: str) -> None:
    if not content or not content.strip():
        _fail("content must be non-empty")
    if len(content) > MAX_CONTENT_CHARS:
        _fail(f"content exceeds {MAX_CONTENT_CHARS} char limit")


def validate_metadata(metadata: dict | None) -> dict | None:
    if metadata is None:
        return None
    import json

    size = len(json.dumps(metadata))
    if size > MAX_METADATA_BYTES:
        _fail(f"metadata JSON exceeds {MAX_METADATA_BYTES} bytes")
    return metadata


def validate_content_type(content_type: str) -> str:
    ct = (content_type or "text").lower()
    if ct not in ALLOWED_CONTENT_TYPES:
        _fail(f"content_type must be one of {sorted(ALLOWED_CONTENT_TYPES)}")
    return ct
