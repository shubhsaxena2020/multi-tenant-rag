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
from ..vector_store import delete_document_chunks, upsert_chunks
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
    # v10.7: a per-tenant override (tenant.chunk_quota) takes precedence over the global
    # default when set (>0); 0 means "inherit global default". Operators set this via the
    # admin-gated endpoint, so a tenant can never raise its own cap.
    tenant = await tenants.get_tenant(tenant_id)
    limit = getattr(tenant, "chunk_quota", 0) or s.tenant_chunk_quota
    if limit <= 0:
        return
    used = await tenants.chunk_count_async(tenant_id)
    if used + n_new > limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"tenant chunk quota exceeded ({used}/{limit}); "
                   f"offboard stale documents or request a quota increase",
            headers={
                "Retry-After": "3600",
                "X-Quota-Limit": str(limit),
                "X-Quota-Used": str(used + n_new),
            },
        )


async def ingest_core(
    tenant_id: str,
    title: str,
    text: str,
    content_type: str = "text",
    metadata: dict | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    acl: list[str] | None = None,
    doc_id: str | None = None,
    source_url: str | None = None,
    source_hash: str | None = None,
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
        # Still record a catalog row so the tenant sees the (empty) doc with its
        # quarantine outcome, and keep idempotency on re-ingest.
        doc_id = doc_id or str(uuid.uuid4())
        await _record_catalog(
            tenant_id, doc_id, title, content_type, 0, source_url, source_hash, acl
        )
        return {"doc_id": doc_id, "title": title,
                "chunk_count": 0, "quarantined_chunks": quarantined}

    # PHASE B.1: idempotent re-ingestion. If a doc_id was supplied and already has chunks
    # in the tenant's collection, remove the prior chunk set so the re-upsert REPLACES
    # (not duplicates) the stale chunks. Cross-tenant scope is guaranteed by the combined
    # (tenant_id, doc_id) filter inside delete_document_chunks.
    if doc_id:
        # Decrement the tenant chunk counter by the EXACT number of chunks we are about
        # to replace, so re-ingestion is net-neutral on quota (otherwise every re-ingest
        # would permanently inflate usage — a quota-accuracy bug).
        prior = delete_document_chunks(tenant_id, doc_id)
        if prior:
            await tenants.increment_chunk_count(tenant_id, -prior)
            # Keep the registry row (upsert_document below updates it in place).

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

    doc_id = doc_id or str(uuid.uuid4())
    base = dict(metadata or {})
    from ..rbac import apply_acl_to_chunk_metadata
    base = apply_acl_to_chunk_metadata(base, acl)
    chunk_ids = upsert_chunks(
        tenant_id=tenant_id, doc_id=doc_id, title=title,
        chunks=texts, dense=dense, sparse=sparse,
        base_metadata=base, content_type=content_type,
    )
    await tenants.increment_chunk_count(tenant_id, len(chunk_ids))
    # PHASE E.2: meter ingestion tokens (embedding input) — deterministic estimate so
    # metering works without an external embedder; fail-open (import broad except).
    try:
        from ..analytics import record_ingest_usage, estimate_tokens

        ingest_tokens = sum(estimate_tokens(t) for t in texts)
        await record_ingest_usage(tenant_id, len(chunk_ids), ingest_tokens)
    except Exception:  # noqa: BLE001 — metering must never fail ingestion
        pass
    await _record_catalog(
        tenant_id, doc_id, title, content_type, len(chunk_ids),
        source_url, source_hash, acl,
    )
    if on_progress:
        on_progress(total, total)
    return {"doc_id": doc_id, "title": title, "chunk_count": len(chunk_ids),
            "quarantined_chunks": quarantined}


async def _record_catalog(
    tenant_id: str,
    doc_id: str,
    title: str,
    content_type: str,
    chunk_count: int,
    source_url: str | None,
    source_hash: str | None,
    acl: list[str] | None,
) -> None:
    from ..db import upsert_document

    await upsert_document(
        tenant_id=tenant_id,
        doc_id=doc_id,
        title=title,
        content_type=content_type,
        chunk_count=chunk_count,
        source_url=source_url,
        source_hash=source_hash,
        acl=acl,
    )


async def ingest_text(
    tenant_id: str, title: str, text: str, content_type: str = "text",
    metadata: dict | None = None, on_progress: Callable[[int, int], None] | None = None,
    acl: list[str] | None = None,
    doc_id: str | None = None,
    source_url: str | None = None,
    source_hash: str | None = None,
) -> dict:
    return await ingest_core(
        tenant_id, title, text, content_type, metadata, on_progress, acl,
        doc_id=doc_id, source_url=source_url, source_hash=source_hash,
    )


async def ingest_url(
    tenant_id: str, url: str, title: str | None = None,
    metadata: dict | None = None, on_progress: Callable[[int, int], None] | None = None,
    acl: list[str] | None = None,
    doc_id: str | None = None,
    source_hash: str | None = None,
) -> dict:
    body = fetch_url(url)
    # PHASE B.1/B.2: idempotent re-crawl. If a source_hash was supplied (sitemap crawler
    # derives one from the URL) and we already have a doc for it, reuse its doc_id so the
    # re-crawl REPLACES the page in place instead of fanning out duplicate chunks.
    if doc_id is None and source_hash:
        from ..db import find_document_by_source_hash

        existing = await find_document_by_source_hash(tenant_id, source_hash)
        if existing:
            doc_id = existing.get("doc_id")
    return await ingest_core(
        tenant_id, title or url, body, "html", metadata, on_progress, acl,
        doc_id=doc_id, source_url=url, source_hash=source_hash,
    )
