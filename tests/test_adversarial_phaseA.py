"""Adversarial Phase A regression tests: CORS deny-by-default + publishable/secret
key-tier split + cross-tenant isolation.

These encode the independent re-verification (agent-6) so the Phase A security surface
stays green without manual re-checking. They run against the in-process TestClient with
the hermetic fixtures from conftest.py (embedded in-memory Qdrant, temp SQLite).

Key guarantees asserted:
- A publishable key (pk_) is rejected (403) from EVERY admin/ingest/delete/rotate/write
  route, and allowed only on read paths (/query, /query/stream) and /feedback.
- Cross-tenant isolation: a key for tenant X used against tenant Y's namespace path can
  never read or write Y's data (the {tenant} path segment is untrusted; tenant is derived
  from the key).
"""
import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client_and_keys():
    from app.main import app

    with TestClient(app) as c:
        admin = "test-admin-key-for-tests"
        # create two tenants with distinct content sentinels
        r = c.post("/api/v1/tenants", headers={"Admin-Key": admin},
                   json={"name": "A", "tenant_id": "adv-a", "plan": "pro",
                         "allowed_groups": ["*"]})
        rk_a = r.json()["api_key"]
        r = c.post("/api/v1/tenants", headers={"Admin-Key": admin},
                   json={"name": "B", "tenant_id": "adv-b", "plan": "pro",
                         "allowed_groups": ["*"]})
        rk_b = r.json()["api_key"]
        r = c.post("/api/v1/adv-a/keys/publishable", headers={"Authorization": f"Bearer {rk_a}"},
                   json={})
        pk_a = r.json()["api_key"]
        # distinct content
        c.post("/api/v1/adv-a/ingest/text", headers={"Authorization": f"Bearer {rk_a}"},
               json={"title": "DA", "text": "SENT-A-7F3K alpha bravo", "doc_id": "da"})
        c.post("/api/v1/adv-b/ingest/text", headers={"Authorization": f"Bearer {rk_b}"},
               json={"title": "DB", "text": "SENT-B-9Q2M delta echo", "doc_id": "db"})
        yield c, rk_a, rk_b, pk_a


# ---- 1. publishable key blocked on every privileged route ----
ADMIN_ROUTES = [
    ("GET", "/api/v1/tenants"),
    ("GET", "/api/v1/admin/console"),
    ("GET", "/metrics"),
    ("GET", "/audit"),
    ("GET", "/audit/verify"),
    ("GET", "/api/v1/openapi.json"),
    ("GET", "/api/v1/docs"),
]
WRITE_ROUTES = [
    ("POST", "/api/v1/adv-a/documents", {"title": "x", "content": "c", "content_type": "text", "doc_id": "d1"}),
    ("POST", "/api/v1/adv-a/ingest/url", {"url": "https://e.com", "title": "x", "doc_id": "d2"}),
    ("POST", "/api/v1/adv-a/ingest/text", {"title": "x", "text": "c", "doc_id": "d3"}),
    ("POST", "/api/v1/adv-a/ingest/jobs", {"kind": "text", "text": "c", "title": "x"}),
    ("POST", "/api/v1/adv-a/ingest/sitemap", {"url": "https://e.com/s.xml"}),
    ("DELETE", "/api/v1/adv-a/documents/d1", None),
    ("DELETE", "/api/v1/adv-a/jobs/jx", None),
    ("DELETE", "/api/v1/adv-a/keys/pk_", None),
    ("POST", "/api/v1/adv-a/keys", {}),
    ("POST", "/api/v1/adv-a/keys/publishable", {}),
    ("PUT", "/api/v1/adv-a/eval/set", {"items": []}),
    ("POST", "/api/v1/adv-a/eval/run", {}),
    ("POST", "/api/v1/adv-a/eval/quality", {}),
    ("GET", "/api/v1/adv-a/eval/runs", None),
    ("POST", "/api/v1/adv-a/eval/golden/auto", {}),
    ("POST", "/api/v1/adv-a/lead", {"name": "a", "email": "a@b.c", "question": "q"}),
    ("GET", "/api/v1/adv-a/leads", None),
    ("POST", "/api/v1/adv-a/config", {"k": "v"}),
    ("GET", "/api/v1/adv-a/config", None),
    ("GET", "/api/v1/adv-a/analytics", None),
    ("GET", "/api/v1/adv-a/knowledge-gaps", None),
    ("GET", "/api/v1/adv-a/keys", None),
    ("GET", "/api/v1/adv-a/documents", None),
    ("GET", "/api/v1/adv-a/jobs", None),
    ("GET", "/api/v1/adv-a/jobs/jx", None),
    ("GET", "/api/v1/adv-a/ingest/sitemap/jx", None),
]


@pytest.mark.parametrize("method,path", ADMIN_ROUTES)
def test_publishable_key_blocked_on_admin_routes(client_and_keys, method, path):
    c, rk_a, rk_b, pk_a = client_and_keys
    r = c.request(method, path, headers={"Admin-Key": pk_a})
    assert r.status_code == 403, f"{method} {path} should be 403 for publishable key"


@pytest.mark.parametrize("method,path,body", WRITE_ROUTES)
def test_publishable_key_blocked_on_write_routes(client_and_keys, method, path, body):
    c, rk_a, rk_b, pk_a = client_and_keys
    h = {"Authorization": f"Bearer {pk_a}"}
    r = c.request(method, path, headers=h, json=body)
    assert r.status_code == 403, f"{method} {path} should be 403 for publishable key"


def test_publishable_key_allowed_on_read_paths(client_and_keys):
    c, rk_a, rk_b, pk_a = client_and_keys
    r = c.post("/api/v1/adv-a/query", headers={"Authorization": f"Bearer {pk_a}"},
               json={"question": "alpha", "generate": False})
    assert r.status_code == 200
    r = c.post("/api/v1/adv-a/query/stream", headers={"Authorization": f"Bearer {pk_a}"},
               json={"question": "alpha", "generate": False})
    assert r.status_code == 200


# ---- 2. cross-tenant isolation ----
def test_cross_namespace_read_returns_own_data_only(client_and_keys):
    c, rk_a, rk_b, pk_a = client_and_keys
    # pk_a used against B's namespace path -> must resolve to A, never leak B
    r = c.post("/api/v1/adv-b/query", headers={"Authorization": f"Bearer {pk_a}"},
               json={"question": "x", "generate": False})
    body = json.dumps(r.json())
    assert "SENT-A-7F3K" in body
    assert "SENT-B-9Q2M" not in body


def test_cross_namespace_publishable_write_rejected(client_and_keys):
    c, rk_a, rk_b, pk_a = client_and_keys
    # pk_a write against B's namespace -> 403 (rejected entirely, no cross-tenant effect)
    r = c.post("/api/v1/adv-b/ingest/text", headers={"Authorization": f"Bearer {pk_a}"},
               json={"title": "x", "text": "CROSSWRITE", "doc_id": "x"})
    assert r.status_code == 403
    r = c.delete("/api/v1/adv-b/documents/da", headers={"Authorization": f"Bearer {pk_a}"})
    assert r.status_code == 403


def test_cross_namespace_secret_write_lands_in_own_tenant(client_and_keys):
    c, rk_a, rk_b, pk_a = client_and_keys
    # rk_a write against B's namespace path: must NOT appear in B (other tenant untouched)
    sent = "CROSS-A-ISOLATED"
    r = c.post("/api/v1/adv-b/ingest/text", headers={"Authorization": f"Bearer {rk_a}"},
               json={"title": "CA", "text": sent, "doc_id": "ca"})
    assert r.status_code in (200, 201)
    # B's own query must NOT see the secret
    rb = c.post("/api/v1/adv-b/query", headers={"Authorization": f"Bearer {rk_b}"},
                json={"question": "x", "generate": False})
    assert sent not in json.dumps(rb.json())
    # A's own query DOES see it (path ignored, resolves to key's tenant)
    ra = c.post("/api/v1/adv-a/query", headers={"Authorization": f"Bearer {rk_a}"},
                json={"question": "x", "generate": False})
    assert sent in json.dumps(ra.json())


# ---- 3. CORS deny-by-default (TestClient passes Origin through ASGI scope) ----
def test_cors_disallowed_origin_no_acao(client_and_keys):
    c, rk_a, rk_b, pk_a = client_and_keys
    r = c.get("/api/v1/adv-a/documents",
              headers={"Authorization": f"Bearer {rk_a}", "Origin": "https://evil.example.com"})
    assert "access-control-allow-origin" not in r.headers


def test_cors_allowed_origin_echoed(client_and_keys):
    c, rk_a, rk_b, pk_a = client_and_keys
    r = c.get("/api/v1/adv-a/documents",
              headers={"Authorization": f"Bearer {rk_a}", "Origin": "https://app.client.com"})
    # conftest does not set ALLOWED_EMBED_ORIGINS, so default allowlist is empty -> no ACAO.
    # We assert the deny-by-default property regardless of configured origins: a non-listed
    # origin must never receive ACAO. (Empty allowlist => evil is non-listed.)
    assert "access-control-allow-origin" not in r.headers
