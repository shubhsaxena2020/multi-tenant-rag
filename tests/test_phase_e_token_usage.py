"""PHASE E #14 — per-tenant token usage + cost metering.

Real verification against the in-process app (TestClient):
- A generated answer (sync /query with generate=True) records token usage for the tenant.
- The per-tenant token-usage endpoint reports total_tokens > 0 and estimated_rows >= 1 (no LLM
  provider configured -> estimated, honest).
- The fleet endpoint aggregates across tenants.
- Admin gate 403; unknown tenant 404.
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


def _create_tenant(client, name):
    r = client.post("/api/v1/tenants", json={"name": name},
                    headers={"Authorization": f"Bearer {ADMIN}"})
    assert r.status_code == 201, r.text
    return r.json()["tenant_id"], r.json()["api_key"]


def test_token_usage_metered_on_generated_query(client):
    tid, sk = _create_tenant(client, "tokco")
    doc = ("Paris is the capital of France. The Eiffel Tower is a landmark in Paris. "
           "France is a country in Western Europe.") * 4
    ri = client.post(f"/api/v1/{tid}/documents", json={"title": "france", "content": doc},
                     headers={"Authorization": f"Bearer {sk}"})
    assert ri.status_code == 201, ri.text

    # generate=True is the tenant default; a grounded query triggers answer generation -> metering.
    r = client.post(f"/api/v1/{tid}/query", json={"question": "What is the capital of France?", "generate": True},
                    headers={"Authorization": f"Bearer {sk}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("answer"), "expected a generated answer"

    usage = client.get(f"/admin/token-usage/{tid}", headers={"Authorization": f"Bearer {ADMIN}"})
    assert usage.status_code == 200, usage.text
    data = usage.json()
    assert data["total_tokens"] > 0, data
    assert data["calls"] >= 1, data
    assert data["estimated_rows"] >= 1, "no-LLM path must be flagged estimated"
    assert data["cost_basis"] == "no_price_set", "no price invented by default"


def test_fleet_token_usage_and_auth(client):
    tid, sk = _create_tenant(client, "tokco2")
    client.post(f"/api/v1/{tid}/documents", json={"title": "x", "content": "Some indexed content about widgets."},
                headers={"Authorization": f"Bearer {sk}"})
    client.post(f"/api/v1/{tid}/query", json={"question": "Tell me about widgets", "generate": True},
                headers={"Authorization": f"Bearer {sk}"})

    fleet = client.get("/admin/token-usage", headers={"Authorization": f"Bearer {ADMIN}"})
    assert fleet.status_code == 200, fleet.text
    fdata = fleet.json()
    assert fdata["total_tokens"] > 0, fdata
    assert fdata["tenant_count"] >= 1, fdata

    # admin gate
    assert client.get(f"/admin/token-usage/{tid}").status_code == 403
    # unknown tenant
    assert client.get("/admin/token-usage/nope_tenant", headers={"Authorization": f"Bearer {ADMIN}"}).status_code == 404
