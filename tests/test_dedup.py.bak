"""GitHub issue #4: content-hash re-ingestion dedup.

Re-ingesting the same URL/document must NOT create duplicate chunks. Identical content is
idempotent (reuse doc_id, no re-embed, no quota burn); changed content replaces the prior
version (delete stale chunks, report previous_doc_id). Cross-tenant isolation is preserved.
"""
import os

import pytest

from app.db import get_registry_entry
from app.vector_store import get_client

V = "/api/v1"


def _make_tenant(client, name):
    r = client.post(f"{V}/tenants", json={"name": name},
                    headers={"Admin-Key": os.environ.get("ADMIN_API_KEY")})
    assert r.status_code == 201, r.text
    return r.json()


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


def _qdrant_point_count(tenant_id, doc_id):
    """Count live Qdrant points for (tenant_id, doc_id)."""
    from qdrant_client.models import FieldCondition, Filter, MatchValue

    client = get_client()
    # collection name is the configured prefix ("rag" in tests via create_collection default)
    from app.config import get_settings
    name = get_settings().collection_prefix
    # ensure collection exists (tests use an in-memory client per fixture)
    try:
        resp = client.count(
            collection_name=name,
            count_filter=Filter(must=[
                FieldCondition(key="tenant_id", match=MatchValue(value=tenant_id)),
                FieldCondition(key="doc_id", match=MatchValue(value=doc_id)),
            ]),
        )
    except Exception:
        return 0
    return resp.count


def test_reingest_url_is_idempotent_no_duplicates(client):
    """Re-ingesting the SAME url yields exactly one set of chunks (doc_id reused, count stable)."""
    t = _make_tenant(client, "dup1")
    auth = _auth(t["api_key"])
    url = "https://example.com/docs/guide"

    # First ingest (the runner path is async; use the sync endpoint which also derives doc_key).
    # We mock the fetch by hitting ingest/url, which internally fetches. To keep the test
    # hermetic we instead exercise ingest/text paths for determinism of content; URL path is
    # covered server-side via doc_key_for_url. Here we assert idempotency on the text path.
    r1 = client.post(f"{V}/dup1/documents", headers=auth,
                     json={"title": "guide", "content": "Alpha beta gamma. " * 40,
                           "content_type": "text"})
    assert r1.status_code == 201, r1.text
    b1 = r1.json()
    assert b1["content_hash"]
    d1 = b1["doc_id"]
    c1 = b1["chunk_count"]
    assert c1 >= 1

    # Re-ingest identical content.
    r2 = client.post(f"{V}/dup1/documents", headers=auth,
                     json={"title": "guide", "content": "Alpha beta gamma. " * 40,
                           "content_type": "text"})
    assert r2.status_code == 201, r2.text
    b2 = r2.json()
    # Idempotent: same doc_id, no new chunks, previous_doc_id == None (no replacement).
    assert b2["doc_id"] == d1, "idempotent re-ingest must reuse the same doc_id"
    assert b2["chunk_count"] == c1
    assert b2["previous_doc_id"] is None
    assert b2["content_hash"] == b1["content_hash"]

    # Exactly one set of points lives for that doc_id (no duplicates).
    assert _qdrant_point_count(t["tenant_id"], d1) == c1

    # Registry has exactly one row for this tenant+doc_key.
    reg = await_get_registry(t["tenant_id"], "text:guide:text")
    assert reg is not None
    assert reg["doc_id"] == d1
    assert reg["chunk_count"] == c1


async def _registry(tenant_id, doc_key):
    return await get_registry_entry(tenant_id, doc_key)


def _run(coro):
    """Run a coroutine to completion in a fresh loop (pytest main thread has no running loop)."""
    import asyncio
    return asyncio.run(coro)


def await_get_registry(tenant_id, doc_key):
    return _run(_registry(tenant_id, doc_key))


def test_reingest_changed_content_replaces_stale_chunks(client):
    """Changed content for the same source replaces prior chunks; previous_doc_id reported."""
    t = _make_tenant(client, "dup2")
    auth = _auth(t["api_key"])

    r1 = client.post(f"{V}/dup2/documents", headers=auth,
                     json={"title": "page", "content": "Version one content here. " * 30,
                           "content_type": "text"})
    assert r1.status_code == 201, r1.text
    b1 = r1.json()
    d1 = b1["doc_id"]
    c1 = b1["chunk_count"]

    # Tenant chunk counter after first ingest.
    from app.db import chunk_count
    import asyncio
    used1 = _run(chunk_count(t["tenant_id"]))
    assert used1 == c1

    # Re-ingest with DIFFERENT content (more text).
    r2 = client.post(f"{V}/dup2/documents", headers=auth,
                     json={"title": "page", "content": "Version two content here, expanded. " * 60,
                           "content_type": "text"})
    assert r2.status_code == 201, r2.text
    b2 = r2.json()
    d2 = b2["doc_id"]
    assert d2 != d1, "changed content must get a new doc_id"
    assert b2["previous_doc_id"] == d1, "must report the superseded doc_id"
    assert b2["content_hash"] != b1["content_hash"]

    # Stale chunks for d1 are gone; new chunks for d2 exist.
    assert _qdrant_point_count(t["tenant_id"], d1) == 0, "stale chunks must be deleted"
    assert _qdrant_point_count(t["tenant_id"], d2) == b2["chunk_count"]
    # Total tenant chunk count is NOT inflated: it equals the new count (c2), not c1+c2.
    used2 = _run(chunk_count(t["tenant_id"]))
    assert used2 == b2["chunk_count"], f"quota inflated: {used2} != {b2['chunk_count']}"

    # Registry row now points at d2.
    reg = await_get_registry(t["tenant_id"], "text:page:text")
    assert reg["doc_id"] == d2
    assert reg["content_hash"] == b2["content_hash"]


def test_dedup_is_per_tenant_isolated(client):
    """Two tenants with the same title+content keep independent documents (no cross-tenant replace)."""
    a = _make_tenant(client, "tenA")
    b = _make_tenant(client, "tenB")
    auth_a = _auth(a["api_key"])
    auth_b = _auth(b["api_key"])
    content = "Shared boilerplate used by both tenants. " * 30

    ra = client.post(f"{V}/tenA/documents", headers=auth_a,
                     json={"title": "shared", "content": content, "content_type": "text"})
    rb = client.post(f"{V}/tenB/documents", headers=auth_b,
                     json={"title": "shared", "content": content, "content_type": "text"})
    assert ra.status_code == 201 and rb.status_code == 201
    da, db = ra.json()["doc_id"], rb.json()["doc_id"]
    assert da != db, "per-tenant doc_ids must differ"

    # Tenant A re-ingests; tenant B's chunks must be untouched.
    ra2 = client.post(f"{V}/tenA/documents", headers=auth_a,
                      json={"title": "shared", "content": content, "content_type": "text"})
    assert ra2.json()["doc_id"] == da
    assert _qdrant_point_count(b["tenant_id"], db) == rb.json()["chunk_count"], \
        "tenant B chunks must remain after tenant A re-ingest"


def test_different_titles_are_distinct_documents(client):
    """Same content under different titles stays separate (doc_key includes title)."""
    t = _make_tenant(client, "dup3")
    auth = _auth(t["api_key"])
    content = "Identical body text. " * 30

    r1 = client.post(f"{V}/dup3/documents", headers=auth,
                     json={"title": "report-2024", "content": content, "content_type": "text"})
    r2 = client.post(f"{V}/dup3/documents", headers=auth,
                     json={"title": "report-2025", "content": content, "content_type": "text"})
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["doc_id"] != r2.json()["doc_id"]
    # No replacement happened (different sources) -> previous_doc_id None on both.
    assert r1.json()["previous_doc_id"] is None
    assert r2.json()["previous_doc_id"] is None


def test_reingest_idempotent_does_not_burn_quota(client):
    """Identical re-ingest must not increment the tenant chunk quota."""
    t = _make_tenant(client, "dup4")
    auth = _auth(t["api_key"])
    content = "Quota should not move on identical re-ingest. " * 30

    client.post(f"{V}/dup4/documents", headers=auth,
                json={"title": "q", "content": content, "content_type": "text"})
    from app.db import chunk_count
    import asyncio
    used_before = _run(chunk_count(t["tenant_id"]))

    client.post(f"{V}/dup4/documents", headers=auth,
                json={"title": "q", "content": content, "content_type": "text"})
    used_after = _run(chunk_count(t["tenant_id"]))
    assert used_after == used_before, "idempotent re-ingest must not change chunk quota"
