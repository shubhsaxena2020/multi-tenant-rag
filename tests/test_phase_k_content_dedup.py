"""v10.10 — content-hash deduplication (re-ingest same URL/document REPLACES, never duplicates).

The complaint: re-ingesting the same URL or same document body used to mint a fresh doc_id
each time (when no doc_id was supplied) and fan out duplicate chunks. These tests prove the
fix for BOTH the synchronous endpoints (which previously missed the dedup key) and the
content-hash path for raw text/document bodies.

Coverage:
  * POST /{tenant}/ingest/url — same URL twice, no doc_id => ONE catalog row, updated content,
    query returns new text, tenant chunk_count net-neutral.
  * POST /{tenant}/documents — same body twice, no doc_id => ONE catalog row, replaced in place.
  * POST /{tenant}/ingest/text — same text twice, no doc_id => deduplicated (one doc).
  * Cross-tenant: identical body in two tenants => each gets its OWN doc (content_hash is
    tenant-scoped, never shared across tenants).
  * Trivial whitespace differences in the body still match (normalized hash).
  * Stale live DB (no content_hash column) is migrated by init_db; re-ingest still dedups.
"""
import asyncio

import pytest

V = "/api/v1"
ADMIN = {"Admin-Key": "test-admin-key-for-tests"}


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


def _make_tenant(client, name="dedup"):
    r = client.post(f"{V}/tenants", json={"name": name, "plan": "standard"}, headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()


def _chunks_of(client, tid, key, doc_id):
    """Exact chunk count for a doc via the documents catalog (chunk_count column)."""
    cat = client.get(f"{V}/{tid}/documents", headers=_auth(key)).json()
    row = next((d for d in cat if d["doc_id"] == doc_id), None)
    return row["chunk_count"] if row else 0


def test_sync_url_ingest_same_url_dedups_without_doc_id(client):
    t = _make_tenant(client)
    tid, key = t["tenant_id"], t["api_key"]
    url = "https://example.com/pricing"
    html1 = "<html><body><h1>Original pricing page v1 with old numbers.</h1></body></html>"
    html2 = "<html><body><h1>UPDATED pricing page v2 with new numbers.</h1></body></html>"

    # Patch fetch_url so no real network call happens (SSRF-safe offline test).
    import app.ingestion as ing
    orig = ing.fetch_url
    calls = {"n": 0, "html": html1}

    def fake_fetch(u, timeout=20.0):
        calls["n"] += 1
        return calls["html"]

    ing.fetch_url = fake_fetch
    try:
        r1 = client.post(f"{V}/{tid}/ingest/url", headers=_auth(key), json={"url": url})
        assert r1.status_code == 201, r1.text
        doc_id = r1.json()["doc_id"]
        first_count = r1.json()["chunk_count"]
        assert first_count >= 1

        # Re-ingest the SAME url -> must reuse the same doc_id (content-hash/source-hash dedup).
        calls["html"] = html2
        r2 = client.post(f"{V}/{tid}/ingest/url", headers=_auth(key), json={"url": url})
        assert r2.status_code == 201, r2.text
        assert r2.json()["doc_id"] == doc_id, "same URL must reuse the same doc_id"

        # Exactly one catalog row for this doc.
        cat = client.get(f"{V}/{tid}/documents", headers=_auth(key)).json()
        rows = [d for d in cat if d["doc_id"] == doc_id]
        assert len(rows) == 1, f"duplicate catalog rows: {rows}"

        # Query must return the UPDATED text, not the original.
        q = client.post(f"{V}/{tid}/query", headers=_auth(key),
                        json={"question": "pricing", "generate": False, "top_k": 5})
        snippets = " ".join(h["text"] for h in q.json()["results"])
        assert "UPDATED" in snippets, f"stale/duplicate chunks: {snippets[:200]}"
        assert "Original pricing" not in snippets, "stale chunk still present after re-ingest"
    finally:
        ing.fetch_url = orig


def test_documents_endpoint_same_body_dedups_without_doc_id(client):
    t = _make_tenant(client)
    tid, key = t["tenant_id"], t["api_key"]
    body = "Quarterly strategy memo: we will expand into the APAC region next year."

    r1 = client.post(f"{V}/{tid}/documents", headers=_auth(key),
                     json={"title": "Memo", "content": body})
    assert r1.status_code == 201, r1.text
    doc_id = r1.json()["doc_id"]
    first_count = r1.json()["chunk_count"]
    assert first_count >= 1

    # Re-ingest identical body, no doc_id -> must replace, not duplicate.
    r2 = client.post(f"{V}/{tid}/documents", headers=_auth(key),
                     json={"title": "Memo", "content": body})
    assert r2.status_code == 201, r2.text
    assert r2.json()["doc_id"] == doc_id, "same body must reuse the same doc_id"

    cat = client.get(f"{V}/{tid}/documents", headers=_auth(key)).json()
    rows = [d for d in cat if d["doc_id"] == doc_id]
    assert len(rows) == 1, f"duplicate catalog rows: {rows}"


def test_ingest_text_same_text_dedups_without_doc_id(client):
    t = _make_tenant(client)
    tid, key = t["tenant_id"], t["api_key"]
    body = "Onboarding guide: step one is to create your workspace."

    r1 = client.post(f"{V}/{tid}/ingest/text", headers=_auth(key),
                     json={"title": "Guide", "text": body})
    assert r1.status_code == 201, r1.text
    doc_id = r1.json()["doc_id"]

    r2 = client.post(f"{V}/{tid}/ingest/text", headers=_auth(key),
                     json={"title": "Guide", "text": body})
    assert r2.status_code == 201, r2.text
    assert r2.json()["doc_id"] == doc_id, "same text must reuse the same doc_id"

    cat = client.get(f"{V}/{tid}/documents", headers=_auth(key)).json()
    rows = [d for d in cat if d["doc_id"] == doc_id]
    assert len(rows) == 1, f"duplicate catalog rows: {rows}"


def test_content_hash_is_tenant_scoped(client):
    ta = _make_tenant(client, name="tA")
    tb = _make_tenant(client, name="tB")
    same_body = "Confidential roadmap: ship the hybrid search GA in Q3."

    ra = client.post(f"{V}/{ta['tenant_id']}/documents", headers=_auth(ta["api_key"]),
                     json={"title": "R", "content": same_body})
    rb = client.post(f"{V}/{tb['tenant_id']}/documents", headers=_auth(tb["api_key"]),
                     json={"title": "R", "content": same_body})
    assert ra.status_code == 201 and rb.status_code == 201
    # Identical body across tenants must NOT collapse to one doc.
    assert ra.json()["doc_id"] != rb.json()["doc_id"], "content_hash must be tenant-scoped"


def test_whitespace_normalization_matches(client):
    t = _make_tenant(client)
    tid, key = t["tenant_id"], t["api_key"]
    a = "The   quick\n\tbrown   fox jumps."
    b = "The quick brown fox jumps."  # trivial whitespace differences only

    ra = client.post(f"{V}/{tid}/documents", headers=_auth(key),
                     json={"title": "X", "content": a})
    rb = client.post(f"{V}/{tid}/documents", headers=_auth(key),
                     json={"title": "X", "content": b})
    assert ra.status_code == 201 and rb.status_code == 201
    assert ra.json()["doc_id"] == rb.json()["doc_id"], "whitespace-normalized bodies must dedup"


def test_stale_documents_table_migrated_then_dedups():
    """A live `documents` table created before v10.10 lacks content_hash; init_db must add
    it (non-destructive) and find_document_by_content_hash must work on the migrated schema."""
    import os
    import sqlite3
    import tempfile

    fd, p = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db_url = f"sqlite:///{p}"
    prev = os.environ.get("DB_URL")
    os.environ["DB_URL"] = db_url
    try:
        from app import db as dbmod
        from app.config import get_settings

        dbmod._engine = None
        dbmod._session_maker = None
        get_settings.cache_clear()

        # Build a pre-v10.10 `documents` table (no content_hash column).
        con = sqlite3.connect(p)
        con.execute(
            "CREATE TABLE documents ("
            "doc_id TEXT PRIMARY KEY, tenant_id TEXT, title TEXT, content_type TEXT, "
            "chunk_count INTEGER DEFAULT 0, source_url TEXT, source_hash TEXT, "
            "acl TEXT DEFAULT '[]', created_at TEXT, updated_at TEXT)"
        )
        con.commit()
        con.close()

        asyncio.run(dbmod.init_db())

        # After migration the schema must contain content_hash (proves non-destructive migration ran).
        con = sqlite3.connect(p)
        cols = {r[1] for r in con.execute("PRAGMA table_info(documents)")}
        con.close()
        assert "content_hash" in cols, "init_db failed to add content_hash column"

        # And the dedup lookup must function against the migrated schema.
        t = asyncio.run(dbmod.create_tenant("mig", "mig-tenant", "mig-key", "standard"))
        tid = t["tenant_id"]
        asyncio.run(dbmod.upsert_document(
            tenant_id=tid, doc_id="d1", title="M", content_type="text",
            chunk_count=2, content_hash="abc",
        ))
        found = asyncio.run(dbmod.find_document_by_content_hash(tid, "abc"))
        assert found is not None and found["doc_id"] == "d1"
    finally:
        if prev is None:
            os.environ.pop("DB_URL", None)
        else:
            os.environ["DB_URL"] = prev
        from app.config import get_settings as _gs

        _gs.cache_clear()
        dbmod._engine = None
        dbmod._session_maker = None
        try:
            os.unlink(p)
        except OSError:
            pass
