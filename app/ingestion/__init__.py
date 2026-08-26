"""Ingestion service: turn raw content/URLs into embedded chunks in a tenant's store.

Flow: fetch (if URL) -> clean to text/markdown -> chunk (token recursive) ->
embed -> upsert into the tenant's Qdrant collection. Deterministic embedder is used
when use_real_embedder is off (tests / no-model envs); the pipeline is identical.

`ingest_core` accepts an optional `on_progress(done, total)` callback so the async
job runner can report status. Synchronous endpoints call it with no callback.
"""
from __future__ import annotations

import re
import uuid

import httpx

from ..embed import get_embedder
from ..vector_store import upsert_chunks
from .chunker import chunk_text

MAX_CHUNK_BATCH = 32  # embed in batches; update progress per batch


def fetch_url(url: str, timeout: float = 20.0) -> str:
    """Fetch a URL and return its text. HTML is stripped to readable text."""
    try:
        from trafilatura import extract  # type: ignore
    except Exception:  # noqa: BLE001 - optional dependency; fall back to regex strip
        extract = None
    resp = httpx.get(
        url, timeout=timeout, follow_redirects=True,
        headers={"User-Agent": "rag-service/1.0"},
    )
    resp.raise_for_status()
    body = resp.text
    if extract is not None:
        clean = extract(body, url=url)
        if clean:
            return clean
    return re.sub(r"<[^>]+>", " ", body)


def ingest_core(
    tenant_id: str,
    title: str,
    text: str,
    content_type: str = "text",
    metadata: dict | None = None,
    on_progress=None,
) -> dict:
    embedder = get_embedder()
    chunks = chunk_text(text)
    texts = [c.text for c in chunks]
    total = len(texts)
    vectors: list[list[float]] = []
    for i in range(0, total, MAX_CHUNK_BATCH):
        batch = texts[i : i + MAX_CHUNK_BATCH]
        vectors.extend(embedder.embed(batch))
        if on_progress:
            on_progress(min(i + MAX_CHUNK_BATCH, total), total)
    doc_id = str(uuid.uuid4())
    base = dict(metadata or {})
    chunk_ids = upsert_chunks(
        tenant_id=tenant_id,
        doc_id=doc_id,
        title=title,
        chunks=texts,
        vectors=vectors,
        base_metadata=base,
        content_type=content_type,
    )
    if on_progress:
        on_progress(total, total)
    return {"doc_id": doc_id, "title": title, "chunk_count": len(chunk_ids)}


def ingest_text(
    tenant_id: str,
    title: str,
    text: str,
    content_type: str = "text",
    metadata: dict | None = None,
    on_progress=None,
) -> dict:
    return ingest_core(tenant_id, title, text, content_type, metadata, on_progress)


def ingest_url(tenant_id: str, url: str, title: str | None = None, metadata: dict | None = None, on_progress=None) -> dict:
    body = fetch_url(url)
    return ingest_core(tenant_id, title or url, body, "html", metadata, on_progress)
