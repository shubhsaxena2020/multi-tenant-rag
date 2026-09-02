"""Issue #XX — sitemap auth verification.

Verifies that sitemap ingestion endpoints require proper authentication.
Unauthenticated requests must be rejected, and valid auth must work.
This mirrors the release auth verification pattern from the fleet backlog.
"""

import pytest

from fastapi.testclient import TestClient

from tests.conftest import client  # noqa: F401 (fixture import for pytest collection)

V = "/api/v1"


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


def _make_tenant(client, name="acme"):
    headers = {"Admin-Key": "test-admin-key-for-tests"}
    r = client.post(f"{V}/tenants", json={"name": name, "plan": "standard"}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


_SITEMAP = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    "<url><loc>https://example.com/a</loc></url>"
    "<url><loc>https://example.com/b</loc></url>"
    "</urlset>"
)


def _fake_fetch(url, timeout=20.0):
    if url.rstrip("/").endswith("robots.txt"):
        return ""
    if url.rstrip("/").endswith("sitemap.xml"):
        return _SITEMAP
    if url.endswith("/a"):
        return "<html><body>Alpha pricing page content here.</body></html>"
    if url.endswith("/b"):
        return "<html><body>Beta documentation content here.</body></html>"
    raise RuntimeError(f"unexpected fetch: {url}")


@pytest.fixture()
def patched_fetch(monkeypatch):
    import app.ingestion.ssrf as ssrf

    monkeypatch.setattr(ssrf, "safe_fetch_url", _fake_fetch)
    yield


def _poll_sitemap_job(client, auth, jid, n=60):
    import time

    for _ in range(n):
        st = client.get(f"{V}/acme/ingest/sitemap/{jid}", headers=auth).json()["status"]
        if st in ("completed", "failed"):
            break
        time.sleep(0.1)
    return client.get(f"{V}/acme/ingest/sitemap/{jid}", headers=auth).json()


def test_sitemap_crawl_ingests_pages_auth_required(client, patched_fetch):
    """Sitemap crawl requires auth; unauthenticated requests return 401/403."""
    t = _make_tenant(client)
    auth = _auth(t["api_key"])

    # First verify that authenticated request works
    r = client.post(
        f"{V}/acme/ingest/sitemap",
        headers=auth,
        json={"url": "https://example.com/sitemap.xml", "max_urls": 10},
    )
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    final = _poll_sitemap_job(client, auth, job_id)
    assert final["status"] == "completed", final
    assert final["urls_ingested"] == 2, final

    # Now verify unauthenticated request is rejected
    r_unauth = client.post(
        f"{V}/acme/ingest/sitemap",
        json={"url": "https://example.com/sitemap.xml", "max_urls": 10},
    )
    assert r_unauth.status_code in (401, 403), f"Expected 401/403, got {r_unauth.status_code}: {r_unauth.text}"


def test_sitemap_crawl_ingests_pages_valid_auth(client, patched_fetch):
    """Sitemap crawl works with valid Bearer auth token."""
    t = _make_tenant(client)
    auth = _auth(t["api_key"])

    r = client.post(
        f"{V}/acme/ingest/sitemap",
        headers=auth,
        json={"url": "https://example.com/sitemap.xml", "max_urls": 10},
    )
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    final = _poll_sitemap_job(client, auth, job_id)
    assert final["status"] == "completed", final
    assert final["urls_ingested"] == 2, final

    cat = client.get(f"{V}/acme/documents", headers=auth).json()
    assert cat["total"] == 2, cat
    titles = {d["title"] for d in cat["items"]}
    assert {"https://example.com/a", "https://example.com/b"} <= titles


def test_sitemap_recrawl_replaces_not_duplicates_auth(client, patched_fetch):
    """Re-crawling the same sitemap with auth must REPLACE pages, never duplicate them."""
    t = _make_tenant(client)
    auth = _auth(t["api_key"])

    payload = {"url": "https://example.com/sitemap.xml", "max_urls": 10}

    def _run():
        r = client.post(f"{V}/acme/ingest/sitemap", headers=auth, json=payload)
        assert r.status_code == 202, r.text
        return r.json()["job_id"]

    _poll_sitemap_job(client, auth, _run())
    cat1 = client.get(f"{V}/acme/documents", headers=auth).json()
    assert cat1["total"] == 2, cat1

    # Second crawl of the same sitemap -> still 2 docs (replaced), not 4.
    _poll_sitemap_job(client, auth, _run())
    cat2 = client.get(f"{V}/acme/documents", headers=auth).json()
    assert cat2["total"] == 2, f"recrawl duplicated docs: {cat2}"


def test_sitemap_unauthenticated_key_rejected(client, patched_fetch):
    """Unauthenticated (no valid auth) sitemap requests are rejected."""
    t = _make_tenant(client)

    # Wrong/bearer key should be rejected
    r = client.post(
        f"{V}/acme/ingest/sitemap",
        headers={"Authorization": "Bearer invalid-token-or-key"},
        json={"url": "https://example.com/sitemap.xml", "max_urls": 10},
    )
    assert r.status_code in (401, 403), f"Expected 401/403, got {r.status_code}: {r.text}"


def test_sitemap_no_auth_header_rejected(client, patched_fetch):
    """Sitemap requests without any auth header are rejected."""
    t = _make_tenant(client)

    r = client.post(
        f"{V}/acme/ingest/sitemap",
        json={"url": "https://example.com/sitemap.xml", "max_urls": 10},
    )
    assert r.status_code in (401, 403), f"Expected 401/403, got {r.status_code}: {r.text}"