"""PHASE G (#35-#39) — plan-gated feature ceilings.

- capabilities_for() resolves a plan to {max_top_k, allow_multi_hop, ...}; unknown plans fall back
  to the free/standard tier (fail-closed: never silently grant paid caps).
- /query enforces top_k cap and multi-hop requirement: a free tenant over the cap or requesting
  multi-hop gets 402 with a clear message.
"""

import pytest

from app.plans import capabilities_for, max_top_k_for, plan_allows_multihop


def test_plan_capabilities_fallback():
    # Unknown/empty plan -> standard tier (fail-closed), not a paid tier.
    assert capabilities_for(None).label == "Standard"
    assert capabilities_for("bogus").label == "Standard"
    assert capabilities_for("free").label == "Standard"
    assert capabilities_for("standard").max_top_k == 10
    assert capabilities_for("pro").allow_multi_hop is True
    assert capabilities_for("enterprise").max_top_k == 50


def test_max_top_k_helper():
    assert max_top_k_for("standard") == 10
    assert max_top_k_for("enterprise") == 50
    assert max_top_k_for("nope") == 10  # fallback


def test_multihop_gating():
    assert plan_allows_multihop("standard") is False
    assert plan_allows_multihop("enterprise") is True
    assert plan_allows_multihop("free") is False


def _make_tenant(client, name, plan="standard"):
    r = client.post(
        "/api/v1/tenants", headers={"Admin-Key": "test-admin-key-for-tests"},
        json={"name": name, "plan": plan},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


def _ingest(client, tid, key, title, content):
    r = client.post(
        f"/api/v1/{tid}/documents", headers=_auth(key),
        json={"title": title, "content": content, "content_type": "text"},
    )
    assert r.status_code == 201, r.text


def test_free_tenant_top_k_over_cap_402(client):
    t = _make_tenant(client, "gatefree", plan="standard")
    _ingest(client, t["tenant_id"], t["api_key"], "d", "The Phoenix project launched in Q1.")
    # standard max_top_k is 10; request 50 -> 402
    q = client.post(
        f"/api/v1/{t['tenant_id']}/query", headers=_auth(t["api_key"]),
        json={"question": "When did Phoenix launch?", "top_k": 50},
    )
    assert q.status_code == 402, q.text
    assert q.json()["error"] == "plan_top_k_limit"


def test_free_tenant_multihop_402(client):
    t = _make_tenant(client, "gatefree2", plan="standard")
    _ingest(client, t["tenant_id"], t["api_key"], "d", "The Phoenix project launched in Q1.")
    q = client.post(
        f"/api/v1/{t['tenant_id']}/query", headers=_auth(t["api_key"]),
        json={"question": "How did Phoenix affect billing?", "top_k": 3, "hops": 3},
    )
    assert q.status_code == 402, q.text
    assert q.json()["error"] == "multi_hop_requires_paid_plan"


def test_enterprise_tenant_within_caps(client):
    t = _make_tenant(client, "gateent", plan="enterprise")
    _ingest(client, t["tenant_id"], t["api_key"], "d", "The Phoenix project launched in Q1 with billing.")
    q = client.post(
        f"/api/v1/{t['tenant_id']}/query", headers=_auth(t["api_key"]),
        json={"question": "When did Phoenix launch?", "top_k": 20, "hops": 2},
    )
    assert q.status_code == 200, q.text
    assert q.json()["hop_count"] == 2
