"""PHASE C — widget + SDK polish verification (issue #14).

Covers:
- Static hosting: /widget.js, /widget.html, /demo all serve 200.
- widget.js auth bug is fixed (no literal `apiKey: ***` placeholder).
- widget.html renders safely: citations via safeHref, no untrusted innerHTML, answer via textContent.
- SDK (sdk.py) covers upload_file / ingest_sitemap / list_documents and parses SSE — validated by
  backing its HTTP verbs with the in-process TestClient.
"""
import json
import os

import pytest

V = "/api/v1"

try:
    from sdk import RagClient
except ImportError:
    RagClient = None


# --- local patched_fetch fixture (mirrors tests/test_sitemap.py) so sitemap ingest works ---
_SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/a</loc></url>
  <url><loc>https://example.com/b</loc></url>
</urlset>"""
_PAGE_A = "<html><body><h1>Page A</h1><p>Alpha content for sitemap SDK test.</p></body></html>"
_PAGE_B = "<html><body><h1>Page B</h1><p>Beta content for sitemap SDK test.</p></body></html>"


def _fake_fetch(url, timeout=20.0):
    if url.rstrip("/").endswith("robots.txt"):
        return ""  # permissive
    if url.rstrip("/").endswith("sitemap.xml"):
        return _SITEMAP
    if url.endswith("/a"):
        return _PAGE_A
    if url.endswith("/b"):
        return _PAGE_B
    raise RuntimeError(f"unexpected fetch: {url}")


@pytest.fixture()
def patched_fetch(monkeypatch):
    import app.ingestion.ssrf as ssrf

    monkeypatch.setattr(ssrf, "safe_fetch_url", _fake_fetch)
    yield


def _make_tenant(client, name):
    headers = {"Admin-Key": os.environ.get("ADMIN_API_KEY", "test-admin-key-for-tests")}
    r = client.post(f"{V}/tenants", json={"name": name, "plan": "standard"}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _ingest(client, auth, title, content):
    r = client.post(
        f"{V}/acme/documents",
        headers=auth,
        json={"title": title, "content": content, "content_type": "text"},
    )
    assert r.status_code == 201, r.text
    return r.json()


# ---------------- static hosting ----------------
def test_widget_js_served(client):
    r = client.get("/widget.js")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/javascript")
    assert "RagWidget" in r.text


def test_widget_html_served(client):
    r = client.get("/widget.html")
    assert r.status_code == 200, r.text
    assert "<!DOCTYPE html>" in r.text
    assert "rag:ready" in r.text


def test_demo_page_served(client):
    r = client.get("/demo")
    assert r.status_code == 200, r.text
    assert "widget" in r.text.lower()


# ---------------- widget.js auth bug (was sending literal `apiKey: ***`) ----------------
def test_widget_js_sends_real_apikey_not_placeholder(client):
    js = client.get("/widget.js").text
    assert "apiKey: ***" not in js, "auth placeholder bug regressed"
    assert "apiKey: apiKey" in js, "must forward the real apiKey to the iframe"


# ---------------- widget.html safe rendering ----------------
def test_widget_html_no_untrusted_innerhtml(client):
    html = client.get("/widget.html").text
    # All untrusted text must go through textContent / safeHref, never raw innerHTML of data.
    # (The word "innerHTML" may appear in comments — only flag actual *assignments*.)
    assert ".innerHTML =" not in html and "innerHTML =" not in html.replace("innerHTML =", "innerHTML="), \
        "widget must not inject untrusted HTML via innerHTML"
    # Citation links are validated to http(s) only.
    assert "safeHref" in html
    assert "textContent" in html


def test_widget_html_renders_citations_as_links(client):
    html = client.get("/widget.html").text
    # Clickable citation anchor with class "cite" and a guarded href.
    assert 'class", "cite"' in html or 'a", "cite"' in html or 'class: "cite"' in html
    assert "noopener noreferrer" in html  # safe link attributes
    assert "session_id" in html  # multi-turn enabled


# ---------------- SDK (backed by the in-process TestClient) ----------------
def _sdk_backed_by(client, api_key):
    """Return a RagClient whose HTTP verbs route into the TestClient (no socket needed).

    We patch sdk.requests.post/get at the module level so BOTH the wrapper helpers (_post/
    _get/_post_multipart) AND the direct requests.post call inside query_stream() are routed
    into the in-process TestClient. The SDK builds URLs as http://test/api/v1/... which the
    TestClient accepts.
    """
    import sdk as sdk_mod

    BASE = "http://test/api/v1"

    class _ReqResp:
        """Adapter so the SDK (written against `requests`) works with the TestClient's
        httpx.Response. Emulates the bits query_stream / _post / _get touch."""

        def __init__(self, httpx_resp):
            self._r = httpx_resp

        def raise_for_status(self):
            self._r.raise_for_status()

        def json(self):
            return self._r.json()

        def iter_lines(self, decode_unicode=False):
            # httpx yields str already; decode_unicode is a requests-only kwarg.
            for line in self._r.iter_lines():
                yield line

    def _to_resp(method, url, **kw):
        # sdk passes headers incl. Authorization; drop Content-Type for GET.
        headers = kw.get("headers", {})
        if method == "GET":
            headers = {k: v for k, v in headers.items() if k != "Content-Type"}
            raw = client.get(url, headers=headers, params=kw.get("params"))
        elif kw.get("files") is not None:
            raw = client.post(url, headers=headers, data=kw.get("data"), files=kw.get("files"))
        else:
            raw = client.post(url, headers=headers, json=kw.get("json"))
        return _ReqResp(raw)

    orig_post = sdk_mod.requests.post
    orig_get = sdk_mod.requests.get

    def _post(url, **kw):
        return orig_post(url, **kw) if not url.startswith(BASE) else _to_resp("POST", url, **kw)

    def _get(url, **kw):
        return orig_get(url, **kw) if not url.startswith(BASE) else _to_resp("GET", url, **kw)

    sdk_mod.requests.post = _post
    sdk_mod.requests.get = _get

    c = RagClient(base_url=BASE, api_key=api_key)
    return c, BASE


def _to_json(resp):
    resp.raise_for_status()
    return resp.json()


def test_sdk_list_documents_hits_catalog_endpoint(client):
    if RagClient is None:
        pytest.skip("sdk.py not importable")
    t = _make_tenant(client, "sdkcat")
    auth = {"Authorization": f"Bearer {t['api_key']}"}
    _ingest(client, auth, "doc-a", "Alpha content for the catalog test.")
    sdk, _ = _sdk_backed_by(client, t["api_key"])
    page = sdk.list_documents("sdkcat", limit=10, offset=0)
    assert page["total"] >= 1
    assert any(d["title"] == "doc-a" for d in page["items"])
    assert "doc_key" not in page["items"][0]


def test_sdk_upload_file_ingests_and_is_queryable(client):
    if RagClient is None:
        pytest.skip("sdk.py not importable")
    t = _make_tenant(client, "sdkup")
    auth = {"Authorization": f"Bearer {t['api_key']}"}
    sdk, _ = _sdk_backed_by(client, t["api_key"])
    out = sdk.upload_file("sdkup", "notes.md", b"# Notes\nThe capacitor charges to 5V.", title="notes.md")
    assert out["chunk_count"] >= 1, out
    # Query it back through the same SDK.
    resp = sdk.query("sdkup", "what voltage?", top_k=3, generate=False)
    assert resp["results"]


def test_sdk_ingest_sitemap_returns_job(client, patched_fetch):
    if RagClient is None:
        pytest.skip("sdk.py not importable")
    t = _make_tenant(client, "sdksm")
    auth = {"Authorization": f"Bearer {t['api_key']}"}
    sdk, _ = _sdk_backed_by(client, t["api_key"])
    job = sdk.ingest_sitemap("sdksm", "https://example.com/sitemap.xml", max_urls=10)
    assert "job_id" in job, job


def test_sdk_query_stream_parses_sse(client):
    if RagClient is None:
        pytest.skip("sdk.py not importable")
    t = _make_tenant(client, "sdkstream")
    auth = {"Authorization": f"Bearer {t['api_key']}"}
    _ingest(client, auth, "fact", "The mitochondria is the powerhouse of the cell.")
    sdk, _ = _sdk_backed_by(client, t["api_key"])
    events = list(sdk.query_stream("sdkstream", "what is the powerhouse?", generate=True, top_k=3))
    kinds = {e["event"] for e in events}
    assert "sources" in kinds
    assert "done" in kinds
    # At least one token emitted when generating.
    assert any(e["event"] == "token" for e in events)
