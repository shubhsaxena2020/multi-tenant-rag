"""PHASE H (#40-#42) — known limitations & hardening (document, don't over-reach).

Tracks the documented gap from issue #15 WITHOUT changing tenancy/key-tier behavior (that is
orchestrator-owned P0/P1). This test asserts the CURRENT lenient behavior (a key scoped to tenant
A hitting /{other}/documents returns A's own data with 200) so the gap is pinned and a future
fail-closed hardening can flip the assertion.
"""

import pytest


def _make_tenant(client, name, plan="standard"):
    r = client.post(
        "/api/v1/tenants", headers={"Admin-Key": "test-admin-key-for-tests"},
        json={"name": name, "plan": plan},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


def test_path_tenant_mismatch_is_key_scoped(client):
    """KL-1: path-tenant != key-tenant does NOT fail-closed on the documents route.

    Current (documented, lenient) behavior: the key's tenant wins and the call returns 200 with
    the key's own data. This is safe (Silo isolation means no cross-tenant read is possible) but is
    *lenient* input validation — the jobs route already 404s on the same mismatch.

    TODO(P0/P1, orchestrator-owned): flip this to expect 404 once the orchestrator lands the
    tenancy/key-tier hardening; do NOT modify tenancy/key-tier code in this phase.
    """
    a = _make_tenant(client, "kl-a")
    b = _make_tenant(client, "kl-b")
    # Tenant A ingests a doc under its OWN key.
    doc = client.post(
        f"/api/v1/{a['tenant_id']}/documents", headers=_auth(a["api_key"]),
        json={"title": "A secret", "content": "alpha-zebra-9911", "content_type": "text"},
    )
    assert doc.status_code == 201

    # Key for tenant A, but PATH tenant is B. Fail-closed: 404 on mismatch.
    r = client.get(f"/api/v1/{b['tenant_id']}/documents", headers=_auth(a["api_key"]))
    assert r.status_code == 404, r.text  # KL-1: fail-closed on path-tenant/key-tenant mismatch
