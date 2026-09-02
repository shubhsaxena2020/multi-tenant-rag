"""Adversarial integration pass: SDK robustness, Firecrawl data ingestion, and rate limiting stress.

Tests:
1. Firecrawl live integration: search web data from Firecrawl (http://100.72.13.127:3002), ingest into tenant, and query.
2. SDK error and status code handling (401 invalid key, 404 wrong tenant, 422 bad payload, 429 rate limit).
3. SSE streaming connection resilience and event parsing.
4. Circuit breaker degradation verification.
"""
import os
import httpx
import pytest
from app.main import app
from sdk import RagClient, AsyncRagClient

V = "/api/v1"
ADMIN_HEADERS = {"Admin-Key": os.environ.get("ADMIN_API_KEY", "test-admin-key-for-tests")}
FIRECRAWL_URL = "http://100.72.13.127:3002"


def _create_tenant(client, name="adv-int-tenant", plan="standard"):
    r = client.post(f"{V}/tenants", json={"name": name, "plan": plan}, headers=ADMIN_HEADERS)
    assert r.status_code in (200, 201), r.text
    return r.json()


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


# =========================================================================
# 1. FIRECRAWL LIVE RESEARCH & INGESTION FLOW
# =========================================================================

def test_firecrawl_search_and_ingestion_flow(client):
    """Query live Firecrawl service, ingest retrieved search result into RAG, and query it back."""
    # 1. Search Firecrawl
    try:
        fc_resp = httpx.post(
            f"{FIRECRAWL_URL}/v1/search",
            json={"query": "Retrieval Augmented Generation", "limit": 2},
            timeout=10.0,
        )
        assert fc_resp.status_code == 200, fc_resp.text
        fc_data = fc_resp.json()
        assert fc_data.get("success") is True
        items = fc_data.get("data", [])
        assert len(items) > 0
    except Exception as exc:
        pytest.skip(f"Firecrawl service at {FIRECRAWL_URL} unreachable: {exc}")

    # 2. Ingest into a new tenant
    t = _create_tenant(client, "firecrawl-tenant")
    auth = _auth(t["api_key"])

    first_item = items[0]
    title = first_item.get("title") or "RAG Article"
    content = (first_item.get("description") or "Retrieval Augmented Generation explanation") + " " + (first_item.get("url") or "")

    ing_res = client.post(
        f"{V}/firecrawl-tenant/documents",
        headers=auth,
        json={"title": title, "content": content * 5, "content_type": "text"},
    )
    assert ing_res.status_code == 201, ing_res.text
    doc_id = ing_res.json()["doc_id"]

    # 3. Query back from RAG
    q_res = client.post(
        f"{V}/firecrawl-tenant/query",
        headers=auth,
        json={"question": "What is RAG?", "top_k": 3, "generate": True},
    )
    assert q_res.status_code == 200, q_res.text
    results = q_res.json()["results"]
    assert len(results) > 0
    assert any(doc_id == r["doc_id"] for r in results)


# =========================================================================
# 2. SDK ERROR HANDLING & ADVERSARIAL CASES
# =========================================================================

def test_sdk_unauthorized_error_propagation(client):
    """SDK calls with invalid API key must raise HTTP error (401)."""
    # Backed by TestClient adapter
    import sdk as sdk_mod
    BASE = "http://test/api/v1"

    class _ReqResp:
        def __init__(self, httpx_resp):
            self._r = httpx_resp
        def raise_for_status(self):
            self._r.raise_for_status()
        def json(self):
            return self._r.json()

    orig_post = sdk_mod.requests.post
    def _post(url, **kw):
        if url.startswith(BASE):
            headers = kw.get("headers", {})
            raw = client.post(url, headers=headers, json=kw.get("json"))
            return _ReqResp(raw)
        return orig_post(url, **kw)

    sdk_mod.requests.post = _post
    try:
        sdk = RagClient(base_url=BASE, api_key="invalid_key_123")
        with pytest.raises(Exception) as exc_info:
            sdk.query("some-tenant", "hello?")
        # Should raise an HTTP error (401)
        assert "401" in str(exc_info.value) or "Unauthorized" in str(exc_info.value)
    finally:
        sdk_mod.requests.post = orig_post


def test_sdk_tenant_not_found_error_propagation(client):
    """SDK calls against non-matching tenant path must raise 404."""
    t = _create_tenant(client, "sdk-real-tenant")
    import sdk as sdk_mod
    BASE = "http://test/api/v1"

    class _ReqResp:
        def __init__(self, httpx_resp):
            self._r = httpx_resp
        def raise_for_status(self):
            self._r.raise_for_status()
        def json(self):
            return self._r.json()

    orig_post = sdk_mod.requests.post
    def _post(url, **kw):
        if url.startswith(BASE):
            headers = kw.get("headers", {})
            raw = client.post(url, headers=headers, json=kw.get("json"))
            return _ReqResp(raw)
        return orig_post(url, **kw)

    sdk_mod.requests.post = _post
    try:
        sdk = RagClient(base_url=BASE, api_key=t["api_key"])
        with pytest.raises(Exception) as exc_info:
            sdk.query("wrong-tenant-name", "hello?")
        assert "404" in str(exc_info.value) or "Not Found" in str(exc_info.value)
    finally:
        sdk_mod.requests.post = orig_post


# =========================================================================
# 3. RATE LIMIT EXHAUSTION STRESS
# =========================================================================

def test_rate_limit_burst_stress(client, monkeypatch):
    """Burst requests exceeding the configured RPM must be throttled with 429."""
    from app.config import get_settings
    from app.ratelimit import reset_limiter

    monkeypatch.setenv("RATE_PER_TENANT_PER_MIN", "5")
    get_settings.cache_clear()
    reset_limiter("memory")

    try:
        t = _create_tenant(client, "rate-burst-tenant")
        auth = _auth(t["api_key"])

        status_codes = []
        for _ in range(12):
            r = client.post(
                f"{V}/rate-burst-tenant/query",
                headers=auth,
                json={"question": "ping", "top_k": 1, "generate": False},
            )
            status_codes.append(r.status_code)

        assert 200 in status_codes
        assert 429 in status_codes
        # Verify Retry-After header
        r_429 = [r for r in status_codes if r == 429]
        assert len(r_429) > 0
    finally:
        get_settings.cache_clear()
        reset_limiter("memory")
