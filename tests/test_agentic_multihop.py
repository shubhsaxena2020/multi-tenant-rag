"""PHASE C (#13-#18) — agentic / multi-hop retrieval.

- Plan-gating: standard tenants are clamped to 1 hop; requesting >1 hop without a multi-hop plan
  is rejected (402). Enterprise/pro tenants may use up to max_hops.
- Multi-hop retrieval reuses retrieve() across hops and merges de-duplicated chunks; hop_count is
  surfaced in the response.
"""

import pytest

from app.retrieval.agentic import (
    plan_allows_multihop,
    max_hops_for,
    retrieve_multi_hop,
    multihop_denied,
)


# ---------------- unit: plan gating ----------------
def test_plan_gating():
    assert plan_allows_multihop("standard") is False
    assert plan_allows_multihop("enterprise") is True
    assert plan_allows_multihop("pro") is True
    # standard clamped to 1 hop regardless of request
    assert max_hops_for("standard", requested=5) == 1
    # enterprise allowed up to requested (capped at default 3)
    assert max_hops_for("enterprise", requested=5) == 3
    assert max_hops_for("enterprise", requested=2) == 2
    # denial detection
    assert multihop_denied("standard", 3) is True
    assert multihop_denied("enterprise", 3) is False
    assert multihop_denied("standard", 1) is False


# ---------------- integration: /query honors hops + plan gate ----------------
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


def test_standard_tenant_multihop_rejected(client):
    t = _make_tenant(client, "stdmultihop", plan="standard")
    _ingest(client, t["tenant_id"], t["api_key"], "a", "Alpha launches the Phoenix project in Q1.")
    q = client.post(
        f"/api/v1/{t['tenant_id']}/query", headers=_auth(t["api_key"]),
        json={"question": "What about Phoenix?", "top_k": 3, "hops": 3},
    )
    assert q.status_code == 402, q.text
    assert q.json()["error"] == "multi_hop_requires_paid_plan"


def test_enterprise_tenant_multihop_runs(client):
    t = _make_tenant(client, "entmultihop", plan="enterprise")
    # Two related passages so a 2-hop query can pull from both.
    _ingest(client, t["tenant_id"], t["api_key"], "phoenix",
            "The Phoenix project is Acme's cloud migration. It began in Q1 with the billing service.")
    _ingest(client, t["tenant_id"], t["api_key"], "billing",
            "The billing service migration cut invoice latency by 40%. It depends on the identity service.")
    q = client.post(
        f"/api/v1/{t['tenant_id']}/query", headers=_auth(t["api_key"]),
        json={"question": "How did the Phoenix project affect billing?", "top_k": 3, "hops": 3},
    )
    assert q.status_code == 200, q.text
    body = q.json()
    # Enterprise plan permits multi-hop: hop_count should reflect the requested depth (capped at 3).
    assert body["hop_count"] == 3, body
    # Multi-hop should surface chunks from both passages (merged, de-duplicated).
    joined = " ".join(h["text"] for h in body["results"])
    assert "Phoenix" in joined
    assert "billing" in joined


def test_single_hop_equivalence(client):
    t = _make_tenant(client, "singlehop", plan="standard")
    _ingest(client, t["tenant_id"], t["api_key"], "doc", "RAG combines retrieval with generation.")
    q = client.post(
        f"/api/v1/{t['tenant_id']}/query", headers=_auth(t["api_key"]),
        json={"question": "What is RAG?", "top_k": 3, "hops": 1},
    )
    assert q.status_code == 200, q.text
    assert q.json()["hop_count"] == 1
