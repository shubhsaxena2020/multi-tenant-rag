"""PHASE C — widget + SDK polish verification.

Covers:
  C-backend: retrieved chunks / SSE sources carry a clickable source_url so the widget
             can render real citations (not a bare "N sources" label).
  C-sdk: a real sdk.py RagClient (sync) + AsyncRagClient with correct eval/query routes,
         parses SSE, and the async client actually awaits.
  C-widget: the loader pushes the REAL api key (no "***" redaction artifact) and supports
            a publishable pk_ key + session_id + theming data-attributes.
"""
import os

import pytest

V = "/api/v1"


def _make_tenant(client, name="acme"):
    headers = {"Admin-Key": os.environ.get("ADMIN_API_KEY", "test-admin-key-for-tests")}
    r = client.post(f"{V}/tenants", json={"name": name, "plan": "standard"}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


# ---------------------------------------------------------------------------
# C-backend: clickable source citations
# ---------------------------------------------------------------------------

def test_query_result_metadata_carries_source_url(client):
    """A doc ingested from a URL should expose its source_url in retrieved chunk metadata,
    so the widget can render a clickable citation rather than a dead 'source' label."""
    t = _make_tenant(client)
    auth = _auth(t["api_key"])
    r = client.post(
        f"{V}/acme/documents", headers=auth,
        json={"title": "Docs", "content": "The pricing page explains our plans in detail.",
              "content_type": "text", "metadata": {"source_url": "https://acme.com/pricing"}},
    )
    assert r.status_code == 201, r.text

    q = client.post(f"{V}/acme/query", headers=auth,
                    json={"question": "pricing", "generate": False, "top_k": 3})
    assert q.status_code == 200, q.text
    res = q.json()["results"]
    assert res, "expected at least one retrieved chunk"
    # The source_url must survive into the retrieved chunk payload.
    has_url = any(h.get("metadata", {}).get("source_url") == "https://acme.com/pricing" for h in res)
    assert has_url, f"source_url missing from retrieved metadata: {res}"


def test_sse_sources_include_clickable_url(client):
    """The SSE `sources` event must include a url so the widget can link citations."""
    t = _make_tenant(client)
    auth = _auth(t["api_key"])
    client.post(
        f"{V}/acme/documents", headers=auth,
        json={"title": "Docs", "content": "The pricing page explains our plans in detail.",
              "content_type": "text", "metadata": {"source_url": "https://acme.com/pricing"}},
    )
    resp = client.post(f"{V}/acme/query/stream", headers=auth,
                       json={"question": "pricing", "generate": True, "top_k": 3})
    assert resp.status_code == 200
    sources_block = [b for b in resp.text.split("\n\n") if b.strip().startswith("event: sources")]
    assert sources_block, "no sources event in SSE stream"
    import json

    data = json.loads(sources_block[0].split("data:", 1)[1].strip())
    assert any(s.get("url") == "https://acme.com/pricing" for s in data), f"source url missing: {data}"


# ---------------------------------------------------------------------------
# C-sdk: real RagClient + AsyncRagClient
# ---------------------------------------------------------------------------

def test_sdk_sync_query_stream_parses(client):
    """sdk.RagClient.query_stream parses a canned SSE stream into typed events."""
    try:
        from sdk import RagClient
    except ImportError:
        pytest.skip("sdk.py not present")
    import requests

    canned = (
        "event: sources\ndata: [{\"title\": \"t\", \"url\": \"https://x/p\"}]\n\n"
        "event: token\ndata: \"Hello \"\n\n"
        "event: token\ndata: \"world\"\n\n"
        "event: done\ndata: {\"tenant_id\": \"x\", \"out_of_scope\": false}\n\n"
    )

    class _Resp:
        status_code = 200
        headers = {"content-type": "text/event-stream"}

        def raise_for_status(self):
            pass

        def iter_lines(self, decode_unicode=True):
            for ln in canned.splitlines():
                yield ln

    orig = requests.post
    requests.post = lambda *a, **k: _Resp()
    try:
        client = RagClient(base_url="http://x/api/v1", api_key="pk_test")
        events = list(client.query_stream("x", "hi"))
    finally:
        requests.post = orig
    kinds = [e["event"] for e in events]
    assert kinds == ["sources", "token", "token", "done"]
    assert "".join(e["data"] for e in events if e["event"] == "token") == "Hello world"


def test_sdk_eval_route_targets_correct_endpoint(client):
    """SDK eval helper must POST to the real /{tenant}/eval/quality route (not a 404 path)."""
    try:
        from sdk import RagClient
    except ImportError:
        pytest.skip("sdk.py not present")
    import requests

    captured = {}

    class _Resp:
        status_code = 200
        headers = {"content-type": "application/json"}

        def raise_for_status(self):
            pass

        @property
        def json(self):
            def _():
                return {"faithfulness": 0.9}
            return _

    def _post(url, *a, **k):
        captured["url"] = url
        return _Resp()

    orig = requests.post
    requests.post = _post
    try:
        client = RagClient(base_url="http://x/api/v1", api_key="rk_test")
        client.run_eval_quality("acme")
    finally:
        requests.post = orig
    assert captured["url"].endswith("/api/v1/acme/eval/quality"), captured


def test_sdk_async_client_awaits(client):
    """AsyncRagClient.query must be awaitable and yield a result dict."""
    try:
        from sdk import AsyncRagClient
    except ImportError:
        pytest.skip("sdk.py not present")
    import asyncio

    class _Resp:
        status_code = 200
        headers = {"content-type": "application/json"}

        def raise_for_status(self):
            pass

        @property
        def json(self):
            def _():
                return {"results": [], "answer": "hi"}
            return _

    captured = {}

    class _Session:
        def __init__(self):
            self.headers = {}
        async def post(self, url, **k):
            captured["url"] = url
            return _Resp()

    async def main():
        client = AsyncRagClient(base_url="http://x/api/v1", api_key="pk_test")
        client._session = _Session()
        return await client.query("acme", "hi")

    out = asyncio.run(main())
    assert out["answer"] == "hi"
    assert captured["url"].endswith("/api/v1/acme/query")


# ---------------------------------------------------------------------------
# C-widget: loader pushes the REAL key (no '***' artifact), supports pk_, theming,
#           and the iframe UI generates a session_id + renders clickable citations.
# ---------------------------------------------------------------------------

def test_widget_loader_served_without_redaction_artifact(client):
    """The loader must NOT contain the literal '***' redaction placeholder (a real bug
    where the api key was replaced with '***' before being pushed to the iframe)."""
    js = client.get("/widget.js")
    assert js.status_code == 200, js.text
    body = js.text
    assert "***" not in body, "widget loader must push the REAL api key, not a '***' placeholder"
    # It must forward the real apiKey to the iframe config.
    assert "apiKey: apiKey" in body
    # Publishable-key support + theming + session are wired.
    assert "pk_" in body
    assert "data-accent" in body and "data-theme" in body


def test_widget_loader_executes_and_pushes_real_publishable_key():
    """Execute the loader in a minimal DOM stub via node and assert it posts the REAL
    (publishable) key to the iframe on rag:ready — proving the '***' artifact is gone."""
    import json as _json
    import subprocess

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    js_path = os.path.join(repo, "app", "static", "widget.js")
    with open(js_path) as f:
        loader_src = f.read()

    harness = r"""
    const captured = [];
    global.location = { origin: "https://shop.example.com" };
    const fakeIframe = {
      style: {}, setAttribute(){}, appendChild(){}, addEventListener(){},
      contentWindow: { postMessage: (msg) => captured.push(msg) },
    };
    global.document = {
      querySelectorAll: () => [{
        getAttribute: (k) => ({
          "data-tenant": "acme",
          "data-api-key": "pk_live_PUBLISHABLEKEY",
          "data-base": "https://rag.example.com/api/v1",
          "data-title": "Acme", "data-theme": "light", "data-accent": "#123456",
        }[k] || null),
      }],
      createElement: () => fakeIframe,
      body: { appendChild() {} },
    };
    global.window = { addEventListener: (ev, cb) => { global.__onmsg = cb; } };
    __LOADER__
    global.__onmsg({ origin: "https://shop.example.com", data: { type: "rag:ready" } });
    console.log(JSON.stringify(captured));
    """.replace("__LOADER__", loader_src)
    proc = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    msgs = _json.loads(proc.stdout.strip().splitlines()[-1])
    cfg = [m for m in msgs if m.get("type") == "rag:config"][0]
    assert cfg["apiKey"] == "pk_live_PUBLISHABLEKEY", "loader must push the REAL key (pk_)"
    assert cfg["apiKey"].startswith("pk_"), "loader should accept a publishable key"
    assert cfg["theme"] == "light" and cfg["accent"] == "#123456", "theming must be forwarded"


def test_widget_html_wires_session_id_and_citations():
    """Structural check of the iframe UI: a stable session_id is generated and sent on the
    stream request, and source urls become clickable <a> links with rel isolation."""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    html_path = os.path.join(repo, "app", "static", "widget.html")
    with open(html_path) as f:
        html_src = f.read()
    assert "sessionId" in html_src, "widget must keep a session_id for multi-turn"
    assert "session_id: sessionId" in html_src, "stream request must carry session_id"
    assert 'createElement("a")' in html_src, "citations must be anchors"
    assert "noopener noreferrer" in html_src, "citation links must be rel-isolated"
    # No raw innerHTML from untrusted text (prevents stored-XSS via answer/source content).
    assert "innerHTML" not in html_src, "widget must not use innerHTML for untrusted text"

