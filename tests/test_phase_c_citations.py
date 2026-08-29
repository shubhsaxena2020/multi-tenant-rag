"""PHASE C — clickable source citations with REAL links (issue #6).

Before this fix, citations for uploaded/text-ingested docs rendered as dead `[n] source`
labels (no href) because the source had no external `source_url`. Now:
- The SSE `sources` event carries doc_id + chunk_id (so the widget can deep-link).
- Every citation is a real link: to `source_url` when present, else to the hosted source
  viewer `/doc/{tenant}/{doc_id}#token=...` (works for uploads too).
- A `GET /{tenant}/documents/{doc_id}` endpoint returns the doc's chunks (powers the viewer).
- A `GET /doc/{tenant}/{doc_id}` page renders the source in-browser.
"""
import os

import pytest

V = "/api/v1"


def _make_tenant(client, name="cite-tenant"):
    headers = {"Admin-Key": os.environ.get("ADMIN_API_KEY", "test-admin-key-for-tests")}
    r = client.post(f"{V}/tenants", json={"name": name, "plan": "standard"}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _ingest(client, tenant, api_key, title, content):
    r = client.post(
        f"{V}/{tenant}/documents",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"title": title, "content": content, "content_type": "text"},
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_sources_event_carries_doc_id_and_chunk_id(client):
    """The sources SSE payload must include doc_id + chunk_id so the widget can deep-link,
    even when there is no external url."""
    t = _make_tenant(client)
    tenant, key = t["tenant_id"], t["api_key"]
    doc = _ingest(client, tenant, key, "Pricing", "The Pro plan costs $49 per month and includes SSO.")
    # Query to trigger the sources event.
    r = client.post(
        f"{V}/{tenant}/query/stream",
        headers={"Authorization": f"Bearer {key}"},
        json={"question": "What does the Pro plan cost?", "generate": False, "top_k": 3, "session_id": "s-cite-1"},
    )
    assert r.status_code == 200, r.text
    body = r.text
    assert "event: sources" in body
    import json

    # Extract the sources data line.
    for part in body.split("\n\n"):
        if part.startswith("event: sources"):
            data = json.loads(part.split("data: ", 1)[1])
            assert data, "expected at least one source"
            assert "doc_id" in data[0], "source must carry doc_id for deep-linking"
            assert data[0]["doc_id"] == doc["doc_id"]
            assert "chunk_id" in data[0]
            break
    else:
        pytest.fail("sources event not found")


def test_get_document_returns_chunks(client):
    """GET /{tenant}/documents/{doc_id} returns the doc's chunks (powers the viewer)."""
    t = _make_tenant(client, "cite-doc")
    tenant, key = t["tenant_id"], t["api_key"]
    doc = _ingest(client, tenant, key, "Refund policy", "Refunds are issued within 14 days of purchase.")
    r = client.get(
        f"{V}/{tenant}/documents/{doc['doc_id']}",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["doc_id"] == doc["doc_id"]
    assert out["title"] == "Refund policy"
    assert out["chunks"], "expected document chunks"
    assert "Refunds" in out["chunks"][0]["text"]


def test_get_document_requires_secret_key(client):
    """The doc endpoint is secret-key gated (a publishable key must NOT open another tenant's content)."""
    t = _make_tenant(client, "cite-auth")
    tenant, key = t["tenant_id"], t["api_key"]
    doc = _ingest(client, tenant, key, "Secret", "classified content")
    # No auth -> 403.
    r = client.get(f"{V}/{tenant}/documents/{doc['doc_id']}")
    assert r.status_code in (401, 403), r.text


def test_document_viewer_page_served(client):
    """GET /doc/{tenant}/{doc_id} serves a real HTML page (citation deep-link target)."""
    t = _make_tenant(client, "cite-view")
    tenant, key = t["tenant_id"], t["api_key"]
    doc = _ingest(client, tenant, key, "Viewer doc", "visible source text")
    r = client.get(f"/doc/{tenant}/{doc['doc_id']}")
    assert r.status_code == 200, r.text
    assert "text/html" in r.headers["content-type"]
    # The page is personalized for this tenant/doc (the widget appends #token=...).
    assert tenant in r.text and doc["doc_id"] in r.text
    assert "document_viewer" in r.text.lower() or "Loading source" in r.text


def test_widget_citations_build_viewer_link_when_no_url(client):
    """Widget source must build a real /doc/...#token= link when the source has no external url."""
    html = client.get("/widget.html").text
    assert "citationHref" in html, "widget must build citation links"
    # Prefer external url, else fall back to hosted viewer (so uploads are clickable too).
    assert "safeHref(s && s.url)" in html or "safeHref(s.url)" in html
    assert "/doc/" in html, "widget must deep-link to the hosted viewer for url-less sources"
    assert "#token=" in html, "viewer link must carry the secret key in the URL fragment"
