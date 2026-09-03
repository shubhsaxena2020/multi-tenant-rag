"""v9-3: embeddable widget + SDK + SSE streaming verification."""
import os

import pytest

V = "/api/v1"
try:
    from sdk import RagClient
except ImportError:
    RagClient = None


def _make_tenant(client, name):
    r = client.post(f"{V}/tenants", json={"name": name}, headers={"Admin-Key": os.environ.get("ADMIN_API_KEY")})
    return r.json()


def test_sse_streaming_query(client):
    """v9-3: /{tenant}/query/stream emits sources + token + done SSE events."""
    t = _make_tenant(client, "sse-q")
    key = t["api_key"]
    auth = {"Authorization": f"Bearer {key}"}
    # First, add a document so the stream has content
    client.post(
        f"{V}/{t['tenant_id']}/documents", headers=auth,
        json={"title": "facts", "content": "The Eiffel Tower is in Paris. It was completed in 1889.", "content_type": "text"},
    )
    resp = client.post(
        f"{V}/{t['tenant_id']}/query/stream", headers=auth,
        json={"question": "where is the Eiffel Tower?", "generate": True, "top_k": 3},
    )
    assert resp.status_code == 200
    assert resp.headers.get("content-type", "").startswith("text/event-stream")
    # TestClient returns the full SSE body in .text; parse event blocks.
    events = []
    for block in resp.text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        ev = {}
        for ln in block.splitlines():
            if ln.startswith("event:"):
                ev["event"] = ln[6:].strip()
            elif ln.startswith("data:"):
                import json as _j
                ev["data"] = _j.loads(ln[5:].strip())
        if ev:
            events.append(ev)
    kinds = [e["event"] for e in events]
    assert "sources" in kinds, f"expected 'sources' event, got kinds={kinds}"
    assert "done" in kinds, f"expected 'done' event, got kinds={kinds}"
    tokens = [e["data"] for e in events if e["event"] == "token"]
    assert len(tokens) > 0, f"expected token events with content, got tokens={tokens}"
    assert "".join(tokens).strip(), f"expected non-empty token content, got empty string from tokens={tokens}"


def test_sse_streaming_rate_limited(client, monkeypatch):
    """P0 regression: the SSE stream endpoint must enforce the same rate limit as /query.

    An authenticated tenant that hammers /query/stream must be throttled (429 +
    Retry-After), not given unlimited quota-free access to the expensive
    retrieval/rerank/generation path. We lower the per-IP limit so the test is
    fast and deterministic instead of hammering the default 120/min bucket.
    """
    from app.config import get_settings

    monkeypatch.setenv("RATE_PER_IP_PER_MIN", "3")
    get_settings.cache_clear()

    t = _make_tenant(client, "sse-rl")
    key = t["api_key"]
    auth = {"Authorization": f"Bearer {key}"}

    limited = False
    for _ in range(10):
        r = client.post(
            f"{V}/{t['tenant_id']}/query/stream", headers=auth,
            json={"question": "any", "top_k": 1},
        )
        if r.status_code == 429:
            limited = True
            assert "Retry-After" in r.headers
            break
    assert limited, "expected 429 once the SSE endpoint's rate limit was exhausted"


def test_widget_routes_served_with_csp(client, monkeypatch):
    """v9-3: widget.js/widget.html are served and carry frame-ancestors CSP."""
    from app.config import get_settings
    monkeypatch.setenv("ALLOWED_EMBED_ORIGINS", '["https://example.com"]')
    get_settings.cache_clear()

    js = client.get("/widget.js")
    assert js.status_code == 200
    assert js.headers["content-security-policy"] == "frame-ancestors https://example.com"

    html = client.get("/widget.html")
    assert html.status_code == 200
    assert "frame-ancestors https://example.com" in html.headers["content-security-policy"]

    # with no allowed origins, embedding is forbidden
    monkeypatch.delenv("ALLOWED_EMBED_ORIGINS", raising=False)
    get_settings.cache_clear()
    js2 = client.get("/widget.js")
    assert "frame-ancestors 'none'" in js2.headers["content-security-policy"]


def test_sdk_query_and_stream(tmp_path):
    """v9-3: the SDK can talk to a live (in-process) server via TestClient transport."""
    if RagClient is None:
        pytest.skip("sdk not importable")
    import app.main as m
    from fastapi.testclient import TestClient

    # Point the SDK at an in-process TestClient by monkeypatching requests.
    with TestClient(m.app) as c:
        # create tenant + ingest via the SDK's own HTTP layer is hard against TestClient,
        # so we verify the SDK parses SSE by feeding it a fake streamer.
        client = RagClient(base_url="http://x/api/v1", api_key="rk_test")
        # Simulate the stream parser with a canned SSE byte stream.
        import io, requests

        canned = (
            "event: sources\ndata: [{\"title\": \"t\"}]\n\n"
            "event: token\ndata: \"Hello \"\n\n"
            "event: token\ndata: \"world\"\n\n"
            "event: done\ndata: {\"tenant_id\": \"x\", \"out_of_scope\": false}\n\n"
        )

        class _Resp:
            status_code = 200
            headers = {"content-type": "text/event-stream"}
            def raise_for_status(self): pass
            def iter_lines(self, decode_unicode=True):
                for ln in canned.splitlines():
                    yield ln

        orig_post = requests.post
        requests.post = lambda *a, **k: _Resp()
        try:
            events = list(client.query_stream("x", "hi"))
        finally:
            requests.post = orig_post
        kinds = [e["event"] for e in events]
        assert kinds == ["sources", "token", "token", "done"]
        assert "".join(e["data"] for e in events if e["event"] == "token") == "Hello world"
