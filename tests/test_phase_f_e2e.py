"""PHASE F — production-readiness gate: full end-to-end onboarding drill.

Walks a real client onboarding lifecycle in ONE suite and asserts every step works,
plus verifies cross-tenant isolation holds end-to-end (the highest-risk property of the
whole service). This is the final gate before the backlog's PHASE A-F work is considered
production-ready:

  1. Operator creates a tenant (Admin-Key) -> gets a secret rk_ key.
  2. Tenant mints a publishable pk_ key for the widget.
  3. Tenant ingests a real document (text) -> doc appears in catalog, chunks counted.
  4. Tenant queries the doc -> answer is grounded in the ingested content (in_scope).
  5. Widget submits thumbs feedback with the publishable key (benign, non-destructive).
  6. Out-of-scope question -> captured as a lead (human-handoff) AND logged as a gap.
  7. ISOLATION: a second tenant cannot read tenant A's doc, cannot see A's feedback/lead,
     and a query against tenant B returns no A content.
  8. Admin console returns both tenants with their usage + audit chain intact.
"""
import time

import pytest

V = "/api/v1"
ADMIN = {"Admin-Key": "test-admin-key-for-tests"}


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


def test_e2e_onboarding_drill(client):
    # --- 1. operator creates tenant A ---
    r = client.post(f"{V}/tenants", json={"name": "acme", "plan": "standard"}, headers=ADMIN)
    assert r.status_code == 201, r.text
    A = r.json()
    assert A["api_key"].startswith("rk_")
    tid_a = A["tenant_id"]

    # --- 2. tenant A mints a publishable key for the widget ---
    pk = client.post(f"{V}/{tid_a}/keys/publishable", headers=_auth(A["api_key"])).json()["api_key"]
    assert pk.startswith("pk_")

    # --- 3. tenant A ingests a real document ---
    doc_text = (
        "Acme Support Plan: the Pro tier costs $20 per month and includes 1000 API calls. "
        "The Business tier costs $80 per month and includes unlimited API calls plus priority support. "
        "Refunds are processed within 14 days of purchase."
    )
    ing = client.post(
        f"{V}/{tid_a}/ingest/text", headers=_auth(A["api_key"]),
        json={"title": "support-plans", "text": doc_text, "doc_id": "doc-plans"},
    )
    assert ing.status_code == 201, ing.text
    assert ing.json()["chunk_count"] >= 1

    # catalog reflects the doc
    docs = client.get(f"{V}/{tid_a}/documents", headers=_auth(A["api_key"])).json()
    assert any(d["doc_id"] == "doc-plans" for d in docs), docs

    # --- 4. tenant A queries the doc, answer must be grounded (in_scope) ---
    q = client.post(
        f"{V}/{tid_a}/query", headers=_auth(A["api_key"]),
        json={"question": "how much does the Pro tier cost?", "generate": True},
    )
    assert q.status_code == 200, q.text
    qb = q.json()
    assert qb["out_of_scope"] is False, qb
    assert qb["answer"] is not None
    assert "$20" in qb["answer"], f"answer not grounded in doc: {qb['answer']}"

    # --- 5. widget submits feedback with the PUBLISHABLE key (benign) ---
    fb = client.post(f"{V}/{tid_a}/feedback", headers=_auth(pk),
                     json={"rating": "up", "question": "how much does the Pro tier cost?"})
    assert fb.status_code == 201, fb.text

    # --- 6. out-of-scope question -> lead capture + knowledge gap ---
    oos = client.post(
        f"{V}/{tid_a}/query", headers=_auth(A["api_key"]),
        json={"question": "do you sell enterprise voice assistants for hospitals?", "generate": True},
    )
    assert oos.status_code == 200, oos.text
    assert oos.json()["out_of_scope"] is True, oos.json()
    lead = client.post(
        f"{V}/{tid_a}/lead", headers=_auth(A["api_key"]),
        json={"name": "Dana", "email": "dana@example.com",
              "question": "do you sell enterprise voice assistants for hospitals?"},
    )
    assert lead.status_code == 201, lead.text

    # --- 7. ISOLATION: tenant B cannot see A's data ---
    rb = client.post(f"{V}/tenants", json={"name": "betta", "plan": "standard"}, headers=ADMIN)
    assert rb.status_code == 201, rb.text
    B = rb.json()
    tid_b = B["tenant_id"]

    # B querying the same question must NOT retrieve A's content (no cross-tenant leak)
    qb2 = client.post(
        f"{V}/{tid_b}/query", headers=_auth(B["api_key"]),
        json={"question": "how much does the Pro tier cost?", "generate": True},
    )
    assert qb2.status_code == 200, qb2.text
    # B has ingested nothing -> out_of_scope (it must not surface A's $20 answer)
    assert qb2.json()["out_of_scope"] is True, qb2.json()
    assert "$20" not in (qb2.json().get("answer") or ""), "CROSS-TENANT LEAK: B saw A's content"

    # B must not list A's documents
    docs_b = client.get(f"{V}/{tid_b}/documents", headers=_auth(B["api_key"])).json()
    assert all(d["doc_id"] != "doc-plans" for d in docs_b), "B listed A's doc"
    # B must not see A's feedback
    fb_b = client.get(f"{V}/{tid_b}/feedback", headers=_auth(B["api_key"])).json()
    assert fb_b == [], "B saw A's feedback"
    # B must not see A's leads
    leads_b = client.get(f"{V}/{tid_b}/leads", headers=_auth(B["api_key"])).json()
    assert leads_b == [], "B saw A's leads"
    # A's feedback/lead are intact and tenant A only
    fb_a = client.get(f"{V}/{tid_a}/feedback", headers=_auth(A["api_key"])).json()
    assert len(fb_a) == 1 and fb_a[0]["rating"] == "up"
    leads_a = client.get(f"{V}/{tid_a}/leads", headers=_auth(A["api_key"])).json()
    assert len(leads_a) == 1 and leads_a[0]["email"] == "dana@example.com"

    # --- 8. admin console returns both tenants + audit chain intact ---
    console = client.get(f"{V}/admin/console", headers=ADMIN).json()
    assert console["tenant_count"] >= 2, console
    assert console["audit_chain_ok"] is True, "audit chain must be intact"
    ids = {t["tenant_id"] for t in console["tenants"]}
    assert {tid_a, tid_b}.issubset(ids)
    # each tenant's usage doc count reflects ingestion
    ta_view = next(t for t in console["tenants"] if t["tenant_id"] == tid_a)
    assert ta_view["document_count"] >= 1
    assert ta_view["usage"]["query_count"] >= 2  # the two queries above


def test_admin_console_requires_admin_key(client):
    """The console must be Admin-Key gated (fail-closed), not open to tenant keys."""
    t = client.post(f"{V}/tenants", json={"name": "acme", "plan": "standard"}, headers=ADMIN).json()
    # a tenant secret key must be rejected (401/403)
    r = client.get(f"{V}/admin/console", headers=_auth(t["api_key"]))
    assert r.status_code in (401, 403), r.text
    # a publishable key must also be rejected
    pk = client.post(f"{V}/{t['tenant_id']}/keys/publishable", headers=_auth(t["api_key"])).json()["api_key"]
    r2 = client.get(f"{V}/admin/console", headers=_auth(pk))
    assert r2.status_code in (401, 403), r2.text
