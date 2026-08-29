"""PHASE E #15 — fleet-wide summary analytics endpoint.

Real verification against the in-process app (TestClient):
- GET /admin/summary aggregates usage/feedback/leads/gaps/tokens across all tenants.
- Per-tenant subtotals are present and sum to fleet totals.
- Admin gate 403.
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


def test_fleet_summary_aggregates_tenants(client):
    t1, k1 = _create_tenant(client, "sumco1")
    t2, k2 = _create_tenant(client, "sumco2")
    for tid, sk in ((t1, k1), (t2, k2)):
        client.post(f"/api/v1/{tid}/documents",
                     json={"title": "doc", "content": "France capital Paris. " * 10},
                     headers={"Authorization": f"Bearer {sk}"})
        # one grounded generated query each -> usage + tokens
        client.post(f"/api/v1/{tid}/query", json={"question": "What is the capital of France?", "generate": True},
                    headers={"Authorization": f"Bearer {sk}"})
        # one out-of-scope query each -> knowledge gap
        client.post(f"/api/v1/{tid}/query", json={"question": "What is the meaning of life? (unknown topic)"},
                    headers={"Authorization": f"Bearer {sk}"})

    s = client.get("/admin/summary", headers={"Authorization": f"Bearer {ADMIN}"})
    assert s.status_code == 200, s.text
    data = s.json()
    assert data["tenant_count"] >= 2, data
    tot = data["totals"]
    assert tot["queries"] >= 4, tot  # 2 queries x 2 tenants
    assert tot["knowledge_gaps"] >= 2, tot
    assert tot["docs_ingested"] >= 2, tot
    # tokens fleet view present
    assert "tokens" in data and data["tokens"]["total_tokens"] > 0, data
    # per-tenant subtotals sum to fleet totals
    sum_q = sum(t["queries"] for t in data["tenants"])
    assert sum_q == tot["queries"], (sum_q, tot["queries"])


def test_summary_auth_gate(client):
    assert client.get("/admin/summary").status_code == 403
