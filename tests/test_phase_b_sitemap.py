"""PHASE B.2 — sitemap.xml crawler verification.

The crawler calls into the SSRF-safe fetcher (app.ingestion.ssrf.safe_fetch_url). To
test the crawl/ingest/idempotency logic without real network egress we monkeypatch that
single function in the sitemap module. This exercises the REAL parsing + crawl + idempotent
replace logic, only stubbing the actual bytes-on-the-wire hop (which is already covered by
the SSRF unit tests elsewhere).
"""
import hashlib

import pytest

V = "/api/v1"


def _make_tenant(client, name="acme"):
    headers = {"Admin-Key": "test-admin-key-for-tests"}
    r = client.post(f"{V}/tenants", json={"name": name, "plan": "standard"}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


# A small urlset sitemap pointing at two pages.
_PAGE_A = "<html><body>Alpha pricing page content here.</body></html>"
_PAGE_B = "<html><body>Beta documentation content here.</body></html>"
_SITEMAP = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    "<url><loc>https://example.com/a</loc></url>"
    "<url><loc>https://example.com/b</loc></url>"
    "</urlset>"
)


def _fake_fetch(url, timeout=20.0):
    # sitemap URL -> sitemap XML; page URL -> page HTML.
    if url.rstrip("/").endswith("sitemap.xml"):
        return _SITEMAP
    if url.endswith("/a"):
        return _PAGE_A
    if url.endswith("/b"):
        return _PAGE_B
    if url.rstrip("/").endswith("robots.txt"):
        return ""  # permissive
    raise RuntimeError(f"unexpected fetch: {url}")


@pytest.fixture()
def patched_fetch(monkeypatch):
    import app.ingestion.ssrf as ssrf

    monkeypatch.setattr(ssrf, "safe_fetch_url", _fake_fetch)
    yield


def test_sitemap_crawl_ingests_pages(client, patched_fetch):
    t = _make_tenant(client)
    auth = _auth(t["api_key"])
    r = client.post(
        f"{V}/acme/ingest/sitemap",
        headers=auth,
        json={"url": "https://example.com/sitemap.xml", "max_urls": 10},
    )
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]

    # Poll until the background crawl completes (in-memory, runs fast).
    import time

    for _ in range(50):
        j = client.get(f"{V}/acme/ingest/sitemap/{job_id}", headers=auth)
        st = j.json()["status"]
        if st in ("completed", "failed"):
            break
        time.sleep(0.1)

    final = client.get(f"{V}/acme/ingest/sitemap/{job_id}", headers=auth).json()
    assert final["status"] == "completed", final
    assert final["urls_ingested"] == 2, final

    # Catalog should now list the two crawled pages.
    cat = client.get(f"{V}/acme/documents", headers=auth).json()
    assert len(cat) == 2, cat
    titles = {d["title"] for d in cat}
    assert {"https://example.com/a", "https://example.com/b"} <= titles


def test_sitemap_recrawl_replaces_not_duplicates(client, patched_fetch):
    """Re-crawling the same sitemap must REPLACE pages, never duplicate them."""
    t = _make_tenant(client)
    auth = _auth(t["api_key"])
    payload = {"url": "https://example.com/sitemap.xml", "max_urls": 10}

    def _run():
        import time

        r = client.post(f"{V}/acme/ingest/sitemap", headers=auth, json=payload)
        assert r.status_code == 202, r.text
        jid = r.json()["job_id"]
        for _ in range(50):
            st = client.get(f"{V}/acme/ingest/sitemap/{jid}", headers=auth).json()["status"]
            if st in ("completed", "failed"):
                break
            time.sleep(0.1)
        return jid

    _run()
    cat1 = client.get(f"{V}/acme/documents", headers=auth).json()
    assert len(cat1) == 2, cat1

    # Second crawl of the same sitemap -> still 2 docs (replaced), not 4.
    _run()
    cat2 = client.get(f"{V}/acme/documents", headers=auth).json()
    assert len(cat2) == 2, f"recrawl duplicated docs: {cat2}"


def test_sitemap_respects_robots_disallow(client, monkeypatch):
    """A Disallow:/b in robots.txt must skip that page (skipped_robots>0)."""
    t = _make_tenant(client)
    auth = _auth(t["api_key"])

    robots = "User-agent: *\nDisallow: /b\n"

    def _fetch(url, timeout=20.0):
        if url.rstrip("/").endswith("robots.txt"):
            return robots
        if url.rstrip("/").endswith("sitemap.xml"):
            return _SITEMAP
        if url.endswith("/a"):
            return _PAGE_A
        if url.endswith("/b"):
            return _PAGE_B
        raise RuntimeError(url)

    import app.ingestion.ssrf as ssrf

    monkeypatch.setattr(ssrf, "safe_fetch_url", _fetch)

    r = client.post(f"{V}/acme/ingest/sitemap", headers=auth,
                    json={"url": "https://example.com/sitemap.xml", "max_urls": 10})
    assert r.status_code == 202, r.text
    jid = r.json()["job_id"]
    import time

    for _ in range(50):
        st = client.get(f"{V}/acme/ingest/sitemap/{jid}", headers=auth).json()["status"]
        if st in ("completed", "failed"):
            break
        time.sleep(0.1)
    final = client.get(f"{V}/acme/ingest/sitemap/{jid}", headers=auth).json()
    assert final["status"] == "completed", final
    assert final.get("urls_ingested") == 1, final
    # No field for skipped_robots on the job out; verify only page A made it.
    cat = client.get(f"{V}/acme/documents", headers=auth).json()
    assert len(cat) == 1 and cat[0]["title"] == "https://example.com/a", cat


# --------------------------------------------------------------------------
# v10.11 — sitemap auto-discovery (onboard by root domain, not exact URL)
# --------------------------------------------------------------------------

import asyncio as _asyncio


def asyncio_run(coro):
    return _asyncio.run(coro)


_CHILD_SITEMAP = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    "<url><loc>https://example.com/b</loc></url>"
    "</urlset>"
)
_INDEX = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    "<sitemap><loc>https://example.com/child-sitemap.xml</loc></sitemap>"
    "</sitemapindex>"
)


def test_resolve_sitemap_honors_robots_hint():
    """resolve_sitemap_url must prefer a robots.txt `Sitemap:` hint over /sitemap.xml."""
    import app.ingestion.sitemap as sm
    import app.ingestion.ssrf as ssrf

    def _fetch(url, timeout=20.0):
        if url.rstrip("/").endswith("robots.txt"):
            return "Sitemap: https://example.com/real-sitemap.xml"
        if url.endswith("real-sitemap.xml"):
            return _SITEMAP  # a valid sitemap
        if url.rstrip("/").endswith("sitemap.xml"):
            return "<html>not a sitemap</html>"  # /sitemap.xml is a decoy
        raise RuntimeError(url)

    orig = ssrf.safe_fetch_url
    ssrf.safe_fetch_url = _fetch
    try:
        resolved = asyncio_run(sm.resolve_sitemap_url("https://example.com"))
        assert resolved == "https://example.com/real-sitemap.xml", resolved
    finally:
        ssrf.safe_fetch_url = orig


def test_resolve_sitemap_falls_back_to_sitemap_xml():
    """With no robots hint, root domain must resolve to /sitemap.xml when it's valid."""
    import app.ingestion.sitemap as sm
    import app.ingestion.ssrf as ssrf

    def _fetch(url, timeout=20.0):
        if url.rstrip("/").endswith("robots.txt"):
            return ""  # no hints
        if url.rstrip("/").endswith("sitemap.xml"):
            return _SITEMAP
        raise RuntimeError(url)

    orig = ssrf.safe_fetch_url
    ssrf.safe_fetch_url = _fetch
    try:
        resolved = asyncio_run(sm.resolve_sitemap_url("https://example.com"))
        assert resolved == "https://example.com/sitemap.xml", resolved
    finally:
        ssrf.safe_fetch_url = orig


def test_resolve_sitemap_raises_when_none_found():
    """A domain with no sitemap anywhere must raise a clear ValueError."""
    import app.ingestion.sitemap as sm
    import app.ingestion.ssrf as ssrf

    def _fetch(url, timeout=20.0):
        if url.rstrip("/").endswith("robots.txt"):
            return ""
        if url.rstrip("/").endswith("sitemap.xml"):
            return "<html>nope</html>"
        raise RuntimeError(url)

    orig = ssrf.safe_fetch_url
    ssrf.safe_fetch_url = _fetch
    try:
        with pytest.raises(ValueError):
            asyncio_run(sm.resolve_sitemap_url("https://example.com"))
    finally:
        ssrf.safe_fetch_url = orig


def test_sitemapindex_recursion(client, patched_fetch):
    """A <sitemapindex> must be recursed into its child sitemap and ingest those pages."""
    import app.ingestion.ssrf as ssrf

    def _fetch(url, timeout=20.0):
        if url.rstrip("/").endswith("robots.txt"):
            return ""
        if url.endswith("child-sitemap.xml"):
            return _CHILD_SITEMAP
        if url.rstrip("/").endswith("sitemap.xml"):
            return _INDEX
        if url.endswith("/a"):
            return _PAGE_A
        if url.endswith("/b"):
            return _PAGE_B
        raise RuntimeError(url)

    orig = ssrf.safe_fetch_url
    ssrf.safe_fetch_url = _fetch
    try:
        t = _make_tenant(client)
        auth = _auth(t["api_key"])
        r = client.post(
            f"{V}/acme/ingest/sitemap", headers=auth,
            json={"url": "https://example.com/sitemap.xml", "max_urls": 10},
        )
        assert r.status_code == 202, r.text
        jid = r.json()["job_id"]
        import time

        for _ in range(50):
            st = client.get(f"{V}/acme/ingest/sitemap/{jid}", headers=auth).json()["status"]
            if st in ("completed", "failed"):
                break
            time.sleep(0.1)
        final = client.get(f"{V}/acme/ingest/sitemap/{jid}", headers=auth).json()
        assert final["status"] == "completed", final
        # The index points at child-sitemap.xml which lists page /b only.
        assert final["urls_ingested"] == 1, final
        cat = client.get(f"{V}/acme/documents", headers=auth).json()
        assert len(cat) == 1 and cat[0]["title"] == "https://example.com/b", cat
    finally:
        ssrf.safe_fetch_url = orig


def test_root_domain_onboard_crawls(client):
    """POST /ingest/sitemap with a bare root domain must auto-discover and crawl."""
    import app.ingestion.ssrf as ssrf

    def _fetch(url, timeout=20.0):
        if url.rstrip("/").endswith("robots.txt"):
            return "Sitemap: https://example.com/sitemap.xml"
        if url.endswith("child-sitemap.xml"):
            return _CHILD_SITEMAP
        if url.rstrip("/").endswith("sitemap.xml"):
            return _SITEMAP
        if url.endswith("/a"):
            return _PAGE_A
        if url.endswith("/b"):
            return _PAGE_B
        raise RuntimeError(url)

    orig = ssrf.safe_fetch_url
    ssrf.safe_fetch_url = _fetch
    try:
        t = _make_tenant(client)
        auth = _auth(t["api_key"])
        r = client.post(
            f"{V}/acme/ingest/sitemap", headers=auth,
            json={"url": "https://example.com", "max_urls": 10},
        )
        assert r.status_code == 202, r.text
        jid = r.json()["job_id"]
        import time

        for _ in range(50):
            st = client.get(f"{V}/acme/ingest/sitemap/{jid}", headers=auth).json()["status"]
            if st in ("completed", "failed"):
                break
            time.sleep(0.1)
        final = client.get(f"{V}/acme/ingest/sitemap/{jid}", headers=auth).json()
        assert final["status"] == "completed", final
        assert final["urls_ingested"] == 2, final
        cat = client.get(f"{V}/acme/documents", headers=auth).json()
        assert len(cat) == 2, cat
    finally:
        ssrf.safe_fetch_url = orig

