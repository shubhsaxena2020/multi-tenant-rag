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

import uuid
from collections.abc import Callable

from .. import tenants
from ..config import get_settings
from ..embed import get_embedder
from ..observability import get_logger
from ..vector_store import upsert_chunks
from .chunker import chunk_text

log = get_logger("ingestion")

MAX_CHUNK_BATCH = 32  # embed in batches; update progress per batch


def fetch_url(url: str, timeout: float = 20.0) -> str:
    """Fetch a tenant-supplied URL with SSRF protections (scheme allowlist, resolve-and-
    reject private/loopback/link-local/metadata ranges, IP-pinned to defeat DNS rebinding,
    per-hop redirect revalidation, size/time caps). Raises HTTPException on unsafe targets."""
    from .ssrf import safe_fetch_url

    return safe_fetch_url(url, timeout=timeout)


async def _enforce_quota(tenant_id: str, n_new: int) -> None:
    from fastapi import HTTPException, status

    s = get_settings()
    if s.tenant_chunk_quota <= 0:
        return
    used = await tenants.chunk_count_async(tenant_id)
    if used + n_new > s.tenant_chunk_quota:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"tenant chunk quota exceeded ({used}/{s.tenant_chunk_quota}); "
                   f"upgrade plan or offboard stale documents",
            headers={"X-Quota-Limit": str(s.tenant_chunk_quota),
                     "X-Quota-Used": str(used + n_new)},
        )


async def ingest_core(
    tenant_id: str,
    title: str,
    text: str,
    content_type: str = "text",
    metadata: dict | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    acl: list[str] | None = None,
) -> dict:
    embedder = get_embedder()
    from ..conversation import detect_injection

    chunks = chunk_text(text)
    # --- Ingest-time injection quarantine (OWASP LLM01:2025 indirect prompt injection) ---
    # A poisoned document is the classic RAG attack: the poison is retrieved verbatim and
    # steers the model. Defense in depth: we quarantine (drop) any chunk whose text matches
    # the injection signature AT THE DOOR, so the poison can never reach the vector store.
    # retrieve-time filtering (main.py) is a second layer; this is the one that guarantees
    # the poison is never indexed in the first place.
    clean_texts: list[str] = []
    quarantined = 0
    for c in chunks:
        if detect_injection(c.text):
            quarantined += 1
            log.warning(
                "ingest_chunk_quarantined",
                extra={"tenant_id": tenant_id, "title": title,
                       "snippet": c.text[:120]},
            )
        else:
            clean_texts.append(c.text)
    texts = clean_texts
    total = len(texts)

    # Quota is enforced on the chunks we actually intend to store (clean count).
    await _enforce_quota(tenant_id, total)
    if total == 0:
        # Every chunk was poisoned (or the doc was empty) — nothing to index.
        return {"doc_id": str(uuid.uuid4()), "title": title,
                "chunk_count": 0, "quarantined_chunks": quarantined}

    dense: list[list[float]] = []
    sparse: list[dict[int, float]] = []
    for i in range(0, total, MAX_CHUNK_BATCH):
        batch = texts[i: i + MAX_CHUNK_BATCH]
        # Prefixing (e.g. E5 query:/passage:) is handled centrally by the embedder so
        # queries and passages stay consistent (v8 #4).
        results = embedder.embed_passages(batch)
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
    await tenants.increment_chunk_count(tenant_id, len(chunk_ids))
    if on_progress:
        on_progress(total, total)
    return {"doc_id": doc_id, "title": title, "chunk_count": len(chunk_ids),
            "quarantined_chunks": quarantined}


async def ingest_text(
    tenant_id: str, title: str, text: str, content_type: str = "text",
    metadata: dict | None = None, on_progress: Callable[[int, int], None] | None = None,
    acl: list[str] | None = None,
) -> dict:
    return await ingest_core(tenant_id, title, text, content_type, metadata, on_progress, acl)


async def ingest_url(
    tenant_id: str, url: str, title: str | None = None,
    metadata: dict | None = None, on_progress: Callable[[int, int], None] | None = None,
    acl: list[str] | None = None,
) -> dict:
    body = fetch_url(url)
    return await ingest_core(tenant_id, title or url, body, "html", metadata, on_progress, acl)
