"""Issue #12 — document catalog listing endpoint.

Covers pagination + true total, single-tenant isolation, and that deletion removes a doc
from the catalog (the registry row is cleaned so GET /documents stays truthful).
"""
import pytest

from app.db import count_documents

V = "/api/v1"


def _make_tenant(client, name="acme"):
    headers = {"Admin-Key": "test-admin-key-for-tests"}
    r = client.post(f"{V}/tenants", json={"name": name, "plan": "standard"}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


def _ingest(client, auth, title, n=1):
    ids = []
    for i in range(n):
        r = client.post(
            f"{V}/acme/documents",
            headers=auth,
            json={"title": f"{title}-{i}", "content": f"Catalog content for {title} {i}. " * 20,
                  "content_type": "text"},
        )
        assert r.status_code == 201, r.text
        ids.append(r.json()["doc_id"])
    return ids


def test_catalog_lists_indexed_docs_with_total(client):
    t = _make_tenant(client)
    auth = _auth(t["api_key"])
    _ingest(client, auth, "doc", n=3)
    r = client.get(f"{V}/acme/documents", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 3, body
    assert len(body["items"]) == 3
    assert body["limit"] == 200 and body["offset"] == 0
    # Tenant-relevant fields only; internal doc_key must NOT be exposed.
    item = body["items"][0]
    assert "doc_key" not in item
    assert set(item.keys()) >= {"doc_id", "title", "content_type", "chunk_count"}


def test_catalog_pagination_limit_offset(client):
    t = _make_tenant(client)
    auth = _auth(t["api_key"])
    _ingest(client, auth, "doc", n=5)
    # Page 1: limit=2 offset=0
    p1 = client.get(f"{V}/acme/documents?limit=2&offset=0", headers=auth).json()
    assert p1["total"] == 5
    assert len(p1["items"]) == 2
    assert p1["limit"] == 2 and p1["offset"] == 0
    # Page 2: limit=2 offset=2
    p2 = client.get(f"{V}/acme/documents?limit=2&offset=2", headers=auth).json()
    assert len(p2["items"]) == 2
    seen1 = {d["doc_id"] for d in p1["items"]}
    seen2 = {d["doc_id"] for d in p2["items"]}
    assert seen1.isdisjoint(seen2), "pages must not overlap"
    # Last page: limit=2 offset=4 -> 1 remaining
    p3 = client.get(f"{V}/acme/documents?limit=2&offset=4", headers=auth).json()
    assert len(p3["items"]) == 1
    # Union of all pages == total
    all_ids = seen1 | seen2 | {d["doc_id"] for d in p3["items"]}
    assert len(all_ids) == 5


def test_catalog_is_single_tenant(client):
    t1 = _make_tenant(client, "acme")
    t2 = _make_tenant(client, "globex")
    a1 = _auth(t1["api_key"])
    a2 = _auth(t2["api_key"])
    _ingest(client, a1, "acme-doc", n=2)
    _ingest(client, a2, "globex-doc", n=3)
    c1 = client.get(f"{V}/acme/documents", headers=a1).json()
    c2 = client.get(f"{V}/globex/documents", headers=a2).json()
    assert c1["total"] == 2
    assert c2["total"] == 3
    t1_titles = {d["title"] for d in c1["items"]}
    t2_titles = {d["title"] for d in c2["items"]}
    assert t1_titles.isdisjoint(t2_titles)


def test_catalog_total_matches_db_count(client):
    import asyncio

    t = _make_tenant(client)
    auth = _auth(t["api_key"])
    _ingest(client, auth, "doc", n=4)
    body = client.get(f"{V}/acme/documents", headers=auth).json()
    # Independent source of truth from the DB layer (async -> run to completion).
    assert body["total"] == asyncio.run(count_documents(t["tenant_id"]))


def test_delete_removes_doc_from_catalog(client):
    """Deleting a document must remove it from GET /documents (registry row cleaned)."""
    t = _make_tenant(client)
    auth = _auth(t["api_key"])
    ids = _ingest(client, auth, "doc", n=2)
    before = client.get(f"{V}/acme/documents", headers=auth).json()
    assert before["total"] == 2

    target = ids[0]
    d = client.delete(f"{V}/acme/documents/{target}", headers=auth)
    assert d.status_code == 200, d.text

    after = client.get(f"{V}/acme/documents", headers=auth).json()
    assert after["total"] == 1, after
    remaining = {d["doc_id"] for d in after["items"]}
    assert target not in remaining
    assert ids[1] in remaining


def test_catalog_does_not_expose_doc_key(client):
    t = _make_tenant(client)
    auth = _auth(t["api_key"])
    _ingest(client, auth, "doc", n=1)
    body = client.get(f"{V}/acme/documents", headers=auth).json()
    assert all("doc_key" not in item for item in body["items"]), body
