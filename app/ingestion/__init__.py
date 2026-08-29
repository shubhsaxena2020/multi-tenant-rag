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

import hashlib
import uuid
from collections.abc import Callable

from .. import tenants
from ..config import get_settings
from ..db import decrement_chunk_count, delete_registry_by_doc_id, get_registry_entry, upsert_registry_entry
from ..embed import get_embedder
from ..observability import get_logger
from ..vector_store import delete_document, upsert_chunks
from .chunker import chunk_text

log = get_logger("ingestion")

MAX_CHUNK_BATCH = 32  # embed in batches; update progress per batch


def doc_key_for_url(url: str) -> str:
    """Stable per-tenant identity for a URL source (GitHub issue #4)."""
    return f"url:{url.strip()}"


def doc_key_for_text(title: str, content_type: str) -> str:
    """Stable per-tenant identity for a title+type source (GitHub issue #4)."""
    return f"text:{title.strip()}:{content_type.strip()}"


def content_hash_of(texts: list[str]) -> str:
    """sha256 over the joined cleaned chunk texts — identical content => identical hash."""
    h = hashlib.sha256()
    h.update("\n".join(texts).encode("utf-8"))
    return h.hexdigest()


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
    doc_key: str | None = None,
    source_url: str | None = None,
) -> dict:
    embedder = get_embedder()
    from ..conversation import detect_injection

    chunks = chunk_text(text, content_type=content_type)
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

    # --- Re-ingestion dedup (GitHub issue #4) ---
    # doc_key is the stable per-tenant identity of this source (URL or title+type). When the
    # caller supplies one, we look up the prior version and can skip (idempotent) or replace
    # (content changed) instead of appending fresh duplicates.
    previous_doc_id: str | None = None
    prior_chunk_count = 0
    prior_hash: str | None = None
    if doc_key is not None:
        prior = await get_registry_entry(tenant_id, doc_key)
        if prior is not None:
            previous_doc_id = prior["doc_id"]
            prior_chunk_count = prior["chunk_count"]
            prior_hash = prior["content_hash"]

    if total == 0:
        # Every chunk was poisoned (or the doc was empty) — nothing to index. If this source
        # was already registered, leave the registry as-is (idempotent: a poisoned doc stays
        # not-indexed). If it was never registered, nothing to record.
        return {"doc_id": previous_doc_id or str(uuid.uuid4()), "title": title,
                "chunk_count": 0, "quarantined_chunks": quarantined,
                "content_hash": prior_hash, "previous_doc_id": previous_doc_id,
                "reingested": False}

    new_hash = content_hash_of(texts)

    # Idempotent re-ingestion: identical cleaned content -> reuse the existing doc_id. No
    # re-embed, no new points, no quota burn, no duplicate. The caller/model gets the same
    # doc_id back so downstream references stay stable. `previous_doc_id` is intentionally
    # NOT set here: nothing was replaced, the prior doc_id is still current.
    if doc_key is not None and previous_doc_id is not None and prior_hash == new_hash:
        log.info(
            "ingest_idempotent_skip",
            extra={"tenant_id": tenant_id, "doc_key": doc_key, "doc_id": previous_doc_id},
        )
        return {"doc_id": previous_doc_id, "title": title, "chunk_count": prior_chunk_count,
                "quarantined_chunks": quarantined, "content_hash": new_hash,
                "previous_doc_id": None, "reingested": False}

    # Quota is enforced on the chunks we actually intend to store (clean count). When replacing
    # a prior version, net quota impact = new - prior (a smaller revision frees headroom).
    await _enforce_quota(tenant_id, max(0, total - prior_chunk_count))

    # Replace-on-change: drop the prior version's chunks before indexing the new one so the
    # source keeps exactly one set of chunks (tenant-scoped delete — never crosses tenants).
    if previous_doc_id is not None:
        delete_document(tenant_id, previous_doc_id)
        await delete_registry_by_doc_id(tenant_id, previous_doc_id)
        if prior_chunk_count:
            await decrement_chunk_count(tenant_id, prior_chunk_count)

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
    if doc_key is not None:
        await upsert_registry_entry(
            tenant_id, doc_key, doc_id,
            title=title, content_type=content_type, source_url=source_url,
            content_hash=new_hash, chunk_count=len(chunk_ids),
        )
    if on_progress:
        on_progress(total, total)
    return {"doc_id": doc_id, "title": title, "chunk_count": len(chunk_ids),
            "quarantined_chunks": quarantined, "content_hash": new_hash,
            "previous_doc_id": previous_doc_id, "reingested": previous_doc_id is not None}


async def ingest_text(
    tenant_id: str, title: str, text: str, content_type: str = "text",
    metadata: dict | None = None, on_progress: Callable[[int, int], None] | None = None,
    acl: list[str] | None = None, doc_key: str | None = None,
) -> dict:
    return await ingest_core(tenant_id, title, text, content_type, metadata, on_progress, acl, doc_key=doc_key)


async def ingest_url(
    tenant_id: str, url: str, title: str | None = None,
    metadata: dict | None = None, on_progress: Callable[[int, int], None] | None = None,
    acl: list[str] | None = None,
) -> dict:
    raw = fetch_url(url)
    from .html_util import html_to_text

    text = html_to_text(raw)
    base_meta = dict(metadata or {})
    base_meta.setdefault("source_url", url)
    return await ingest_core(
        tenant_id, title or url, text, "html", base_meta, on_progress, acl,
        doc_key=doc_key_for_url(url), source_url=url,
    )
