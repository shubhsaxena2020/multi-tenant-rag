"""Ingestion service: raw content/URLs → chunked → embedded (dense+sparse) → stored.

Flow: fetch (if URL) -> clean to text -> chunk (token recursive) -> embed (dense+sparse)
-> upsert into the tenant's isolated, encrypted Qdrant collection. Deterministic
embedder (no model) is used when use_real_embedder is off, so the full hybrid pipeline
is verifiable in CI.

`ingest_core` enforces the tenant's chunk quota (fleet protection) and increments the
registry counter used for quota accounting. Optional `on_progress(done,total)` drives
the async job runner.
"""
from __future__ import annotations

import re
import uuid

import httpx

from .. import tenants
from ..config import get_settings
from ..embed import get_embedder
from ..vector_store import upsert_chunks
from .chunker import chunk_text

MAX_CHUNK_BATCH = 32  # embed in batches; update progress per batch


def fetch_url(url: str, timeout: float = 20.0) -> str:
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


def _enforce_quota(tenant_id: str, n_new: int) -> None:
    from fastapi import HTTPException, status

    s = get_settings()
    if s.tenant_chunk_quota <= 0:
        return
    used = tenants.chunk_count(tenant_id)
    if used + n_new > s.tenant_chunk_quota:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"tenant chunk quota exceeded ({used}/{s.tenant_chunk_quota}); "
                   f"upgrade plan or offboard stale documents",
            headers={"X-Quota-Limit": str(s.tenant_chunk_quota),
                     "X-Quota-Used": str(used + n_new)},
        )


def ingest_core(
    tenant_id: str,
    title: str,
    text: str,
    content_type: str = "text",
    metadata: dict | None = None,
    on_progress=None,
    acl: list[str] | None = None,
) -> dict:
    embedder = get_embedder()
    chunks = chunk_text(text)
    texts = [c.text for c in chunks]
    total = len(texts)

    _enforce_quota(tenant_id, total)

    dense: list[list[float]] = []
    sparse: list[dict[int, float]] = []
    for i in range(0, total, MAX_CHUNK_BATCH):
        batch = texts[i: i + MAX_CHUNK_BATCH]
        results = embedder.embed(batch)
        dense.extend(r.dense for r in results)
        sparse.extend(r.sparse for r in results)
        if on_progress:
            on_progress(min(i + MAX_CHUNK_BATCH, total), total)

    doc_id = str(uuid.uuid4())
    base = dict(metadata or {})
    from ..rbac import apply_acl_to_chunk_metadata
    base = apply_acl_to_chunk_metadata(base, acl)
    chunk_ids = upsert_chunks(
        tenant_id=tenant_id, doc_id=doc_id, title=title,
        chunks=texts, dense=dense, sparse=sparse,
        base_metadata=base, content_type=content_type,
    )
    tenants.increment_chunk_count(tenant_id, len(chunk_ids))
    if on_progress:
        on_progress(total, total)
    return {"doc_id": doc_id, "title": title, "chunk_count": len(chunk_ids)}


def ingest_text(
    tenant_id: str, title: str, text: str, content_type: str = "text",
    metadata: dict | None = None, on_progress=None, acl: list[str] | None = None,
) -> dict:
    return ingest_core(tenant_id, title, text, content_type, metadata, on_progress, acl)


def ingest_url(tenant_id: str, url: str, title: str | None = None, metadata: dict | None = None, on_progress=None, acl: list[str] | None = None) -> dict:
    body = fetch_url(url)
    return ingest_core(tenant_id, title or url, body, "html", metadata, on_progress, acl)
