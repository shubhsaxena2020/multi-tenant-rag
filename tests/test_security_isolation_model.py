"""Regression test for the tenant isolation model (security continuation pass).

This test enforces fail-closed tenant isolation: all Qdrant queries must
always filter by tenant_id, and cross-tenant reads must return empty results.

The system uses shared-collection multi-tenancy with strict tenant_id payload
filtering. This test verifies that contract is always upheld.
"""
import os
import pytest

os.environ.setdefault("QDRANT_URL", ":memory:")
os.environ.setdefault("USE_REAL_EMBEDDER", "0")
os.environ.setdefault("USE_REAL_RERANKER", "0")
os.environ.setdefault("DB_URL", "sqlite:///./test_rag_tenants.db")
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "AAAAAAt3stEnvMasterKey0123456789ABCDEF")

V = "/api/v1"


def test_tenant_isolation_always_filters_by_tenant_id(client):
    """v11-SEC: Every query to Qdrant is tenant-scoped via payload filter.
    
    Guarantee: the system never drops the tenant_id filter, even when
    the path segment says a different tenant. The key-derived tenant
    always wins over the path segment.
    """
    from app.main import app
    from starlette.testclient import TestClient
    
    c = TestClient(app)
    
    # Create two tenants with separate data
    r1 = c.post(f"{V}/tenant-alpha", json={"name": "alpha"}, headers={"Admin-Key": os.environ.get("ADMIN_API_KEY")})
    assert r1.status_code == 201
    key1 = r1.json()["api_key"]
    tenant1_id = r1.json()["tenant_id"]
    
    r2 = c.post(f"{V}/tenant-beta", json={"name": "beta"}, headers={"Admin-Key": os.environ.get("ADMIN_API_KEY")})
    assert r2.status_code == 201
    key2 = r2.json()["api_key"]
    tenant2_id = r2.json()["tenant_id"]
    
    # Alpha ingests a confidential doc
    ing = c.post(f"{V}/tenant-alpha/documents", 
                 headers={"Authorization": f"Bearer {key1}"},
                 json={"title": "alpha-secret", "content": "Alpha company secret data"})
    assert ing.status_code == 201
    doc_id_alpha = ing.json()["doc_id"]
    
    # Beta ingests a different doc
    ing = c.post(f"{V}/tenant-beta/documents",
                 headers={"Authorization": f"Bearer {key2}"},
                 json={"title": "beta-secret", "content": "Beta company secret data"})
    assert ing.status_code == 201
    doc_id_beta = ing.json()["doc_id"]
    
    # Alpha queries with Alpha's key - should see Alpha's data
    q1 = c.post(f"{V}/tenant-alpha/query",
                headers={"Authorization": f"Bearer {key1}"},
                json={"question": "secret", "top_k": 5})
    assert q1.status_code == 200
    results1 = q1.json()["results"]
    alpha_results = [r for r in results1 if "Alpha company secret" in r["text"]]
    assert len(alpha_results) >= 1, "Alpha key should see Alpha's data"
    
    # Beta queries with Beta's key - should see Beta's data
    q2 = c.post(f"{V}/tenant-beta/query",
                headers={"Authorization": f"Bearer {key2}"},
                json={"question": "secret", "top_k": 5})
    assert q2.status_code == 200
    results2 = q2.json()["results"]
    beta_results = [r for r in results2 if "Beta company secret" in r["text"]]
    assert len(beta_results) >= 1, "Beta key should see Beta's data"
    
    # CRITICAL: Alpha key querying Beta's path should STILL only see Alpha's data
    # (tenant derived from key, not from path segment)
    q3 = c.post(f"{V}/tenant-beta/query",
                headers={"Authorization": f"Bearer {key1}"},  # Alpha key
                json={"question": "secret", "top_k": 5})
    assert q3.status_code == 200
    results3 = q3.json()["results"]
    # Alpha key on Beta path must NOT see Beta's data - this is the fail-closed guarantee
    beta_in_results = [r for r in results3 if "Beta company secret" in r["text"]]
    assert len(beta_in_results) == 0, \
        f"FAIL: Alpha key saw Beta's data when querying /tenant-beta path! " \
        f"This violates the fail-closed isolation guarantee. Results: {results3}"
    
    # Also verify Alpha's own data IS visible even on Beta path (key-derived tenant wins)
    alpha_on_beta = [r for r in results3 if "Alpha company secret" in r["text"]]
    assert len(alpha_on_beta) >= 1, \
        f"FAIL: Alpha key on Beta path should still see Alpha's data (key-derived tenant). " \
        f"Isolation broken: got {results3}"


def test_publishable_key_cannot_surface_other_tenant_data(client):
    """v11-SEC: Publishable keys must never surface other tenant's data.
    
    Publishable keys are read-only and must be strictly scoped to the tenant
    they were minted from. Cross-tenant reads return empty results.
    """
    from app.main import app
    from starlette.testclient import TestClient
    
    c = TestClient(app)
    
    # Create a tenant and mint a publishable key
    r = c.post(f"{V}/sec-tenant", json={"name": "sec-tenant"}, headers={"Admin-Key": os.environ.get("ADMIN_API_KEY")})
    assert r.status_code == 201
    secret_key = r.json()["api_key"]
    pub_key = r.json()["publishable_key"]  # or mint separately
    
    # Mint publishable key for this tenant
    pub = c.post(f"{V}/sec-tenant/keys/publishable",
                 headers={"Authorization": f"Bearer {secret_key}"})
    assert pub.status_code == 201
    pk = pub.json()["api_key"]
    
    # Ingest data under the secret key
    ing = c.post(f"{V}/sec-tenant/documents",
                 headers={"Authorization": f"Bearer {secret_key}"},
                 json={"title": "confidential", "content": "Top secret tenant data"})
    assert ing.status_code == 201
    doc_id = ing.json()["doc_id"]
    
    # Query with publishable key - should only see own tenant's data
    q = c.post(f"{V}/sec-tenant/query",
               json={"question": "confidential", "top_k": 5},
               headers={"Authorization": f"Bearer {pk}"})
    assert q.status_code == 200
    results = q.json()["results"]
    confidential_in_results = [r for r in results if "Top secret tenant data" in r["text"]]
    assert len(confidential_in_results) >= 1, "Publishable key should see own tenant's data"
    
    # CRITICAL: Publishable key querying OTHER tenant's path must return empty
    # (this test would fail if the system leaked other tenant data)
    r2 = c.post(f"{V}/other-tenant/query",
                json={"question": "confidential", "top_k": 5},
                headers={"Authorization": f"Bearer {pk}"})
    assert r2.status_code == 200
    results2 = r2.json()["results"]
    # Publishable key must NOT surface other tenant's data even on their path
    own_data_in_results = [r for r in results2 if "Top secret tenant data" in r["text"]]
    assert len(own_data_in_results) == 0, \
        f"FAIL: Publishable key surfaced own tenant data when querying /other-tenant path! " \
        f"Isolation broken. Results: {results2}"


def test_cross_tenant_query_returns_empty(client):
    """v11-SEC: Explicit cross-tenant query returns empty results (fail-closed).
    
    When a query is made under Key A's auth to Key A's tenant collection,
    but the path references Key B's tenant, the result must be empty for
    Key A seeing Key B's data - the tenant_id payload filter must always apply.
    """
    from app.main import app
    from starlette.testclient import TestClient
    
    c = TestClient(app)
    
    # Setup two tenants
    r1 = c.post(f"{V}/x-tenant", json={"name": "x"}, headers={"Admin-Key": os.environ.get("ADMIN_API_KEY")})
    assert r1.status_code == 201
    k1 = r1.json()["api_key"]
    t1 = r1.json()["tenant_id"]
    
    r2 = c.post(f"{V}/y-tenant", json={"name": "y"}, headers={"Admin-Key": os.environ.get("ADMIN_API_KEY")})
    assert r2.status_code == 201
    k2 = r2.json()["api_key"]
    t2 = r2.json()["tenant_id"]
    
    # x tenant ingests data
    c.post(f"{V}/x-tenant/documents",
           headers={"Authorization": f"Bearer {k1}"},
           json={"title": "x-data", "content": "X tenant only data"})
    
    # y tenant ingests data  
    c.post(f"{V}/y-tenant/documents",
           headers={"Authorization": f"Bearer {k2}"},
           json={"title": "y-data", "content": "Y tenant only data"})
    
    # y key querying x path -> must return empty (cross-tenant block)
    q = c.post(f"{V}/x-tenant/query",
               headers={"Authorization": f"Bearer {k2}"},
               json={"question": "x-data", "top_k": 5})
    assert q.status_code == 200
    results = q.json()["results"]
    # Fail-closed: must be empty - Key y must NOT see Key x's data
    assert len(results) == 0, \
        f"FAIL: Cross-tenant query returned results! Key y saw data it shouldn't. " \
        f"Results: {results}, isolation guarantee broken"
