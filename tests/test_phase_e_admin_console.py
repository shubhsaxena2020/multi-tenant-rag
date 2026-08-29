"""PHASE E #16 — lightweight admin console.

Real verification:
- GET /admin/console serves a real HTML page (no curl needed), references the API + Admin-Key.
- The data endpoints the page depends on work: /tenants (now includes branding), /{tenant}/documents
  (items list), /admin/summary, /admin/token-usage/{tenant}, /admin/knowledge-gaps/{tenant}.
- Fail-closed: /tenants without Admin-Key -> 403.
"""
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.main import app  # noqa: E402

ADMIN = os.environ.get("ADMIN_API_KEY", "test-admin-key-for-tests")


@pytest.fixture
def client():
    return TestClient(app)


def _create_tenant(client, name, branding=None):
    body = {"name": name}
    if branding:
        body["branding"] = branding
    r = client.post("/api/v1/tenants", json=body, headers={"Authorization": f"Bearer {ADMIN}"})
    assert r.status_code == 201, r.text
    return r.json()["tenant_id"], r.json()["api_key"]


def test_admin_console_page_served(client):
    r = client.get("/admin/console")
    assert r.status_code == 200, r.text
    assert "text/html" in r.headers["content-type"]
    body = r.text
    # The page wires to the API and uses the Admin-Key header (no curl).
    assert "/api/v1" in body
    assert "Admin-Key" in body
    assert "RAG Service — Admin Console" in body


def test_console_data_endpoints_work(client):
    tid, sk = _create_tenant(client, "consoleco", branding={"accent": "#123456", "logo_url": "https://x/y.png"})
    client.post(f"/api/v1/{tid}/documents", json={"title": "manual", "content": "Onboarding steps for new clients. " * 8},
                headers={"Authorization": f"Bearer {sk}"})
    client.post(f"/api/v1/{tid}/query", json={"question": "How do I onboard?", "generate": True},
                headers={"Authorization": f"Bearer {sk}"})

    # tenants list now carries branding (widget config shown in console)
    tl = client.get("/api/v1/tenants", headers={"Authorization": f"Bearer {ADMIN}"})
    assert tl.status_code == 200, tl.text
    tenants = tl.json()
    match = next(t for t in tenants if t["tenant_id"] == tid)
    assert match["branding"].get("accent") == "#123456", match

    # document catalog shape used by the console (items + chunk_count)
    cat = client.get(f"/api/v1/{tid}/documents", headers={"Authorization": f"Bearer {sk}"})
    assert cat.status_code == 200, cat.text
    items = cat.json()["items"]
    assert any(d["title"] == "manual" and d["chunk_count"] > 0 for d in items), items

    # analytics + token usage + gaps (console panels)
    assert client.get(f"/admin/analytics/{tid}", headers={"Authorization": f"Bearer {ADMIN}"}).status_code == 200
    assert client.get(f"/admin/token-usage/{tid}", headers={"Authorization": f"Bearer {ADMIN}"}).status_code == 200
    assert client.get(f"/admin/knowledge-gaps/{tid}", headers={"Authorization": f"Bearer {ADMIN}"}).status_code == 200
    assert client.get("/admin/summary", headers={"Authorization": f"Bearer {ADMIN}"}).status_code == 200


def test_console_endpoints_fail_closed(client):
    # tenants list is admin-only
    assert client.get("/api/v1/tenants").status_code == 403
    # summary is admin-only
    assert client.get("/admin/summary").status_code == 403
