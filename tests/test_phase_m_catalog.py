"""v10.13 — document catalog listing endpoint (GET /{tenant}/documents).

The endpoint + DocumentCatalogOut model already exist (PHASE B.4). This suite proves the
tenant can actually SEE what's indexed: correct shape/fields, tenant-scoped isolation, the
listed docs are genuinely retrievable (catalog reflects real chunks, not phantom rows),
limit param, and empty-tenant behavior.
"""
import pytest

V = "/api/v1"
ADMIN = {"Admin-Key": "test-admin-key-for-tests"}


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


def _make_tenant(client, name="cat"):
    r = client.post(f"{V}/tenants", json={"name": name, "plan": "standard"}, headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()


def test_catalog_lists_ingested_docs_with_fields(client):
    """Ingested docs appear in the catalog with the expected fields and chunk counts."""
    t = _make_tenant(client)
    auth = _auth(t["api_key"])
    r1 = client.post(
        f"{V}/{t['tenant_id']}/documents", headers=auth,
        json={"title": "Pricing", "content": "Pricing page body with details.", "doc_id": "d1"},
    )
    assert r1.status_code == 201, r1.text
    r2 = client.post(
        f"{V}/{t['tenant_id']}/documents", headers=auth,
        json={"title": "About", "content": "About us page body.", "doc_id": "d2"},
    )
    assert r2.status_code == 201, r2.text

    cat = client.get(f"{V}/{t['tenant_id']}/documents", headers=auth)
    assert cat.status_code == 200, cat.text
    rows = cat.json()
    assert len(rows) == 2, rows
    by_id = {d["doc_id"]: d for d in rows}
    assert "d1" in by_id and "d2" in by_id
    for d in rows:
        assert set(d.keys()) >= {
            "doc_id", "title", "content_type", "chunk_count",
            "source_url", "source_hash", "acl", "created_at", "updated_at",
        }
        assert d["chunk_count"] >= 1
        assert d["content_type"] == "text"
        assert d["acl"] == ["__public__"]  # default public


def test_catalog_is_tenant_scoped(client):
    """Tenant B must NOT see tenant A's indexed documents."""
    ta = _make_tenant(client, name="catA")
    tb = _make_tenant(client, name="catB")
    au = _auth(ta["api_key"])
    bu = _auth(tb["api_key"])
    client.post(
        f"{V}/{ta['tenant_id']}/documents", headers=au,
        json={"title": "Secret", "content": "tenant A only content here.", "doc_id": "a1"},
    )
    client.post(
        f"{V}/{tb['tenant_id']}/documents", headers=bu,
        json={"title": "Other", "content": "tenant B only content here.", "doc_id": "b1"},
    )
    cat_a = client.get(f"{V}/{ta['tenant_id']}/documents", headers=au).json()
    cat_b = client.get(f"{V}/{tb['tenant_id']}/documents", headers=bu).json()
    assert [d["doc_id"] for d in cat_a] == ["a1"]
    assert [d["doc_id"] for d in cat_b] == ["b1"]


def test_catalog_rows_are_actually_retrievable(client):
    """What's listed in the catalog must be queryable — catalog reflects real indexed chunks."""
    t = _make_tenant(client)
    auth = _auth(t["api_key"])
    client.post(
        f"{V}/{t['tenant_id']}/documents", headers=auth,
        json={"title": "Onboarding", "content": "Onboarding step one is workspace creation.", "doc_id": "o1"},
    )
    cat = client.get(f"{V}/{t['tenant_id']}/documents", headers=auth).json()
    assert any(d["doc_id"] == "o1" and d["chunk_count"] >= 1 for d in cat)

    q = client.post(
        f"{V}/{t['tenant_id']}/query", headers=auth,
        json={"question": "onboarding workspace", "generate": False, "top_k": 5},
    )
    snippets = " ".join(h["text"] for h in q.json()["results"])
    assert "workspace" in snippets, f"listed doc not retrievable: {snippets[:200]}"


def test_catalog_limit_param(client):
    """The limit query param bounds the number of returned rows."""
    t = _make_tenant(client)
    auth = _auth(t["api_key"])
    for i in range(3):
        client.post(
            f"{V}/{t['tenant_id']}/documents", headers=auth,
            json={"title": f"Doc{i}", "content": f"body number {i} content.", "doc_id": f"x{i}"},
        )
    limited = client.get(f"{V}/{t['tenant_id']}/documents", headers=auth, params={"limit": 2})
    assert limited.status_code == 200, limited.text
    assert len(limited.json()) == 2, limited.json()


def test_catalog_empty_tenant(client):
    """A tenant with nothing indexed gets an empty list (not 404)."""
    t = _make_tenant(client)
    auth = _auth(t["api_key"])
    cat = client.get(f"{V}/{t['tenant_id']}/documents", headers=auth)
    assert cat.status_code == 200, cat.text
    assert cat.json() == []


def test_catalog_reflects_deletion(client):
    """After deleting a doc, it disappears from the catalog."""
    t = _make_tenant(client)
    auth = _auth(t["api_key"])
    client.post(
        f"{V}/{t['tenant_id']}/documents", headers=auth,
        json={"title": "Temp", "content": "temporary doc body.", "doc_id": "tmp"},
    )
    before = client.get(f"{V}/{t['tenant_id']}/documents", headers=auth).json()
    assert any(d["doc_id"] == "tmp" for d in before)

    d = client.delete(f"{V}/{t['tenant_id']}/documents/tmp", headers=auth)
    assert d.status_code == 200, d.text
    after = client.get(f"{V}/{t['tenant_id']}/documents", headers=auth).json()
    assert all(d["doc_id"] != "tmp" for d in after)
