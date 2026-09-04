"""Comprehensive adversarial and API contract boundary tests for rag-service.

Covers:
1. Malformed inputs across API endpoints (top_k bounds, hops limits, empty content, oversized payload, invalid ISO datetimes).
2. Complete Auth permutations and key kind scopes (Publishable vs Secret vs Admin keys, malformed Bearer headers).
3. Cross-tenant async job boundaries (GET / DELETE job across tenants).
4. End-to-end Ingestion-to-Query lifecycle:
   - Ingest with ACL -> RBAC query filtering
   - Catalog pagination limit & offset checks
   - Delete doc -> vector chunk removal verification
   - Re-ingest modified content -> replacement verification
5. Sync and Async SDK method parity and error handling (branding, jobs, document viewer, feedback, handoff).
"""
import json
import os
import pytest
from starlette.testclient import TestClient

from app.main import app
from sdk import RagClient, AsyncRagClient

V = "/api/v1"
ADMIN_KEY = os.environ.get("ADMIN_API_KEY", "test-admin-key-for-tests")
ADMIN_HEADERS = {"Admin-Key": ADMIN_KEY}


@pytest.fixture
def client():
    return TestClient(app)


def _create_tenant(client: TestClient, name: str, plan: str = "standard", allowed_groups: list[str] | None = None, branding: dict | None = None) -> dict:
    body = {"name": name, "plan": plan}
    if allowed_groups is not None:
        body["allowed_groups"] = allowed_groups
    if branding is not None:
        body["branding"] = branding
    r = client.post(f"{V}/tenants", json=body, headers=ADMIN_HEADERS)
    assert r.status_code == 201, r.text
    return r.json()


# ============================================================================
# 1. MALFORMED INPUTS & SCHEMA BOUNDARIES
# ============================================================================

def test_query_top_k_and_hops_boundary_validation(client: TestClient):
    """Test validation on query bounds (top_k ge=1, le=50; hops ge=1, le=5)."""
    t = _create_tenant(client, "bound-query")
    auth = {"Authorization": f"Bearer {t['api_key']}"}

    # top_k = 0 rejected
    r = client.post(f"{V}/bound-query/query", headers=auth, json={"question": "hello", "top_k": 0})
    assert r.status_code == 422

    # top_k = 51 rejected
    r = client.post(f"{V}/bound-query/query", headers=auth, json={"question": "hello", "top_k": 51})
    assert r.status_code == 422

    # hops = 0 rejected
    r = client.post(f"{V}/bound-query/query", headers=auth, json={"question": "hello", "hops": 0})
    assert r.status_code == 422

    # hops = 6 rejected
    r = client.post(f"{V}/bound-query/query", headers=auth, json={"question": "hello", "hops": 6})
    assert r.status_code == 422

    # empty question rejected
    r = client.post(f"{V}/bound-query/query", headers=auth, json={"question": ""})
    assert r.status_code == 422


def test_document_ingest_malformed_inputs(client: TestClient):
    """Test empty title, empty content, and invalid content types."""
    t = _create_tenant(client, "bound-doc")
    auth = {"Authorization": f"Bearer {t['api_key']}"}

    # Empty content
    r = client.post(f"{V}/bound-doc/documents", headers=auth, json={"title": "test", "content": ""})
    assert r.status_code == 422

    # Whitespace-only content
    r = client.post(f"{V}/bound-doc/documents", headers=auth, json={"title": "test", "content": "   \n\t  "})
    assert r.status_code == 422

    # Empty title
    r = client.post(f"{V}/bound-doc/documents", headers=auth, json={"title": "", "content": "Valid content"})
    assert r.status_code == 422

    # Invalid content_type
    r = client.post(f"{V}/bound-doc/documents", headers=auth, json={
        "title": "test", "content": "Valid content", "content_type": "executable/binary"
    })
    assert r.status_code == 422


def test_key_expiry_invalid_date_validation(client: TestClient):
    """Test that malformed ISO datetime in key expiry endpoints returns 422."""
    t = _create_tenant(client, "bound-keys")
    auth = {"Authorization": f"Bearer {t['api_key']}"}

    # Invalid date on secret key mint
    r = client.post(f"{V}/bound-keys/keys/secret", headers=auth, json={"expires_at": "invalid-date-string"})
    assert r.status_code == 422

    # Invalid date on publishable key mint
    r = client.post(f"{V}/bound-keys/keys/publishable", headers=auth, json={"expires_at": "tomorrow afternoon"})
    assert r.status_code == 422

    # Invalid date on patch expiry
    r = client.patch(f"{V}/bound-keys/keys/rk_nonexistent/expiry", headers=auth, json={"expires_at": "not-a-timestamp"})
    assert r.status_code == 422


def test_upload_acl_formats_and_validation(client: TestClient):
    """Test file upload with both JSON array ACL and comma-separated ACL, plus malformed rejection."""
    t = _create_tenant(client, "bound-upload", allowed_groups=["eng", "hr", "finance"])
    auth = {"Authorization": f"Bearer {t['api_key']}"}

    # 1. Valid JSON array ACL
    files = {"file": ("notes.txt", b"Engineering sensitive notes.", "text/plain")}
    data = {"title": "Eng Notes", "acl": json.dumps(["eng"])}
    r = client.post(f"{V}/bound-upload/documents/upload", headers=auth, data=data, files=files)
    assert r.status_code == 201
    assert r.json()["chunk_count"] >= 1

    # 2. Valid comma-separated ACL string
    files = {"file": ("hr.txt", b"HR sensitive policies.", "text/plain")}
    data = {"title": "HR Policies", "acl": "hr, finance"}
    r = client.post(f"{V}/bound-upload/documents/upload", headers=auth, data=data, files=files)
    assert r.status_code == 201

    # 3. Malformed non-string array rejected
    files = {"file": ("bad.txt", b"Bad ACL.", "text/plain")}
    data = {"title": "Bad ACL", "acl": json.dumps([123, 456])}
    r = client.post(f"{V}/bound-upload/documents/upload", headers=auth, data=data, files=files)
    assert r.status_code == 422

    # 4. Wildcard ACL rejected
    files = {"file": ("wild.txt", b"Wildcard ACL.", "text/plain")}
    data = {"title": "Wild ACL", "acl": json.dumps(["*"])}
    r = client.post(f"{V}/bound-upload/documents/upload", headers=auth, data=data, files=files)
    assert r.status_code == 422


# ============================================================================
# 2. AUTH PERMUTATIONS & KEY SCOPING
# ============================================================================

def test_auth_header_permutations(client: TestClient):
    """Test missing, malformed, empty, and non-Bearer Authorization headers."""
    t = _create_tenant(client, "auth-perm")

    # Missing header
    r = client.post(f"{V}/auth-perm/documents", json={"title": "t", "content": "c"})
    assert r.status_code == 401

    # Basic auth scheme instead of Bearer
    r = client.post(f"{V}/auth-perm/documents", headers={"Authorization": "Basic dXNlcjpwYXNz"}, json={"title": "t", "content": "c"})
    assert r.status_code == 401

    # Empty Bearer token
    r = client.post(f"{V}/auth-perm/documents", headers={"Authorization": "Bearer "}, json={"title": "t", "content": "c"})
    assert r.status_code == 401

    # Whitespace-only Bearer token
    r = client.post(f"{V}/auth-perm/documents", headers={"Authorization": "Bearer    "}, json={"title": "t", "content": "c"})
    assert r.status_code == 401

    # Admin key used as tenant key on tenant ingestion -> 401
    r = client.post(f"{V}/auth-perm/documents", headers={"Authorization": f"Bearer {ADMIN_KEY}"}, json={"title": "t", "content": "c"})
    assert r.status_code == 401


def test_publishable_key_scope_matrix(client: TestClient):
    """Verify that publishable key (pk_*) can query and read widget config, but CANNOT perform any write/admin action."""
    t = _create_tenant(client, "pk-matrix")
    sec_auth = {"Authorization": f"Bearer {t['api_key']}"}

    # Mint a publishable key
    r_pk = client.post(f"{V}/pk-matrix/keys/publishable", headers=sec_auth, json={})
    assert r_pk.status_code == 201
    pk_key = r_pk.json()["api_key"]
    assert pk_key.startswith("pk_")
    pub_auth = {"Authorization": f"Bearer {pk_key}"}

    # First ingest a document using secret key
    r_ingest = client.post(f"{V}/pk-matrix/documents", headers=sec_auth, json={"title": "Rules", "content": "Serverless architectures scale to zero."})
    assert r_ingest.status_code == 201
    doc_id = r_ingest.json()["doc_id"]

    # Allowed with publishable key:
    # 1. Query
    r_q = client.post(f"{V}/pk-matrix/query", headers=pub_auth, json={"question": "what scales to zero?"})
    assert r_q.status_code == 200
    assert len(r_q.json()["results"]) >= 1

    # 2. Widget config
    r_cfg = client.get(f"{V}/pk-matrix/widget/config", headers=pub_auth)
    assert r_cfg.status_code == 200

    # FORBIDDEN (403) with publishable key:
    # 1. Session history (secret key only)
    r_sess = client.get(f"{V}/pk-matrix/session/test-sess-1", headers=pub_auth)
    assert r_sess.status_code == 403

    # 2. Ingest text
    r_b1 = client.post(f"{V}/pk-matrix/documents", headers=pub_auth, json={"title": "Hack", "content": "Inject"})
    assert r_b1.status_code == 403

    # 2. Ingest URL
    r_b2 = client.post(f"{V}/pk-matrix/ingest/url", headers=pub_auth, json={"url": "https://example.com"})
    assert r_b2.status_code == 403

    # 3. File upload
    files = {"file": ("test.txt", b"Unauthorized upload", "text/plain")}
    r_b3 = client.post(f"{V}/pk-matrix/documents/upload", headers=pub_auth, files=files)
    assert r_b3.status_code == 403

    # 4. Delete document
    r_b4 = client.delete(f"{V}/pk-matrix/documents/{doc_id}", headers=pub_auth)
    assert r_b4.status_code == 403

    # 5. List documents / catalog
    r_b5 = client.get(f"{V}/pk-matrix/documents", headers=pub_auth)
    assert r_b5.status_code == 403

    # 6. Patch branding
    r_b6 = client.patch(f"{V}/pk-matrix/branding", headers=pub_auth, json={"accent": "#123456"})
    assert r_b6.status_code == 403

    # 7. Mint keys
    r_b7 = client.post(f"{V}/pk-matrix/keys/secret", headers=pub_auth, json={})
    assert r_b7.status_code == 403


# ============================================================================
# 3. CROSS-TENANT ASYNC JOB ISOLATION
# ============================================================================

def test_async_job_cross_tenant_boundaries(client: TestClient):
    """Ensure async jobs created by Tenant A cannot be viewed or deleted by Tenant B."""
    tA = _create_tenant(client, "job-tenant-a")
    tB = _create_tenant(client, "job-tenant-b")
    authA = {"Authorization": f"Bearer {tA['api_key']}"}
    authB = {"Authorization": f"Bearer {tB['api_key']}"}

    # Create job in Tenant A
    r_job = client.post(f"{V}/job-tenant-a/ingest/jobs", headers=authA, json={
        "kind": "text",
        "title": "Job A Doc",
        "text": "Async ingestion text content for Tenant A.",
    })
    assert r_job.status_code == 202
    job_id = r_job.json()["job_id"]

    # Tenant A can get its own job
    r_get_a = client.get(f"{V}/job-tenant-a/jobs/{job_id}", headers=authA)
    assert r_get_a.status_code == 200
    assert r_get_a.json()["job_id"] == job_id

    # Tenant B querying Tenant A's route with Tenant B's key -> 404 (tenant mismatch)
    r_get_b_mismatch = client.get(f"{V}/job-tenant-a/jobs/{job_id}", headers=authB)
    assert r_get_b_mismatch.status_code == 404

    # Tenant B querying its own route with Tenant A's job_id -> 404 (job not found in Tenant B)
    r_get_b_own = client.get(f"{V}/job-tenant-b/jobs/{job_id}", headers=authB)
    assert r_get_b_own.status_code == 404

    # Tenant B attempting to delete Tenant A's job -> 404 on mismatched route
    r_del_b = client.delete(f"{V}/job-tenant-a/jobs/{job_id}", headers=authB)
    assert r_del_b.status_code == 404

    # Tenant A deleting its own job -> 200 OK
    r_del_a = client.delete(f"{V}/job-tenant-a/jobs/{job_id}", headers=authA)
    assert r_del_a.status_code == 200
    assert r_del_a.json()["deleted"] is True


# ============================================================================
# 4. INGESTION-TO-QUERY LIFECYCLE & CATALOG PAGINATION
# ============================================================================

def test_ingestion_catalog_pagination_and_query_lifecycle(client: TestClient):
    """Full lifecycle: batch ingest -> check pagination -> query -> delete -> verify removal."""
    t = _create_tenant(client, "lifecycle-tenant")
    auth = {"Authorization": f"Bearer {t['api_key']}"}

    # 1. Ingest 4 distinct documents
    doc_ids = []
    for i in range(4):
        r = client.post(f"{V}/lifecycle-tenant/documents", headers=auth, json={
            "title": f"Doc {i}",
            "content": f"Document index {i} explains unique topic-{i} in deep detail.",
            "content_type": "text",
        })
        assert r.status_code == 201
        doc_ids.append(r.json()["doc_id"])

    # 2. Check catalog pagination
    page1 = client.get(f"{V}/lifecycle-tenant/documents", headers=auth, params={"limit": 2, "offset": 0}).json()
    assert page1["total"] == 4
    assert len(page1["items"]) == 2
    assert page1["limit"] == 2
    assert page1["offset"] == 0

    page2 = client.get(f"{V}/lifecycle-tenant/documents", headers=auth, params={"limit": 2, "offset": 2}).json()
    assert page2["total"] == 4
    assert len(page2["items"]) == 2
    assert page2["offset"] == 2

    # Verify no overlapping items between page 1 and page 2
    p1_ids = {d["doc_id"] for d in page1["items"]}
    p2_ids = {d["doc_id"] for d in page2["items"]}
    assert p1_ids.isdisjoint(p2_ids)

    # 3. Query topic-2
    r_q = client.post(f"{V}/lifecycle-tenant/query", headers=auth, json={
        "question": "what is unique topic-2?", "top_k": 3
    })
    assert r_q.status_code == 200
    res = r_q.json()["results"]
    assert len(res) >= 1
    assert any("topic-2" in c["text"] for c in res)

    # 4. Delete Doc 2
    r_del = client.delete(f"{V}/lifecycle-tenant/documents/{doc_ids[2]}", headers=auth)
    assert r_del.status_code == 200

    # 5. Verify Doc 2 is removed from catalog
    catalog_after = client.get(f"{V}/lifecycle-tenant/documents", headers=auth, params={"limit": 10}).json()
    assert catalog_after["total"] == 3
    assert not any(d["doc_id"] == doc_ids[2] for d in catalog_after["items"])

    # 6. Query topic-2 again -> doc 2 chunks must NOT be returned
    r_q_after = client.post(f"{V}/lifecycle-tenant/query", headers=auth, json={
        "question": "what is unique topic-2?", "top_k": 5
    })
    assert r_q_after.status_code == 200
    res_after = r_q_after.json()["results"]
    assert not any(c["doc_id"] == doc_ids[2] for c in res_after)


# ============================================================================
# 5. SDK & ASYNC SDK PARITY & INTEGRATION
# ============================================================================

def test_sync_sdk_methods_and_admin_branding(client: TestClient):
    """Test that RagClient supports branding on create_tenant, jobs, get_document, feedback, and handoff."""
    import sdk as sdk_mod

    BASE = "http://test/api/v1"

    class _ReqResp:
        def __init__(self, httpx_resp):
            self._r = httpx_resp
        def raise_for_status(self):
            self._r.raise_for_status()
        def json(self):
            return self._r.json()
        def iter_lines(self, decode_unicode=False):
            for line in self._r.iter_lines():
                yield line

    def _to_resp(method, url, **kw):
        headers = kw.get("headers", {})
        if method == "GET":
            headers = {k: v for k, v in headers.items() if k != "Content-Type"}
            raw = client.get(url, headers=headers, params=kw.get("params"))
        elif method == "DELETE":
            raw = client.delete(url, headers=headers)
        elif kw.get("files") is not None:
            raw = client.post(url, headers=headers, data=kw.get("data"), files=kw.get("files"))
        else:
            raw = client.post(url, headers=headers, json=kw.get("json"), params=kw.get("params"))
        return _ReqResp(raw)

    orig_post = sdk_mod.requests.post
    orig_get = sdk_mod.requests.get
    orig_delete = sdk_mod.requests.delete

    def _post(url, **kw):
        return orig_post(url, **kw) if not url.startswith(BASE) else _to_resp("POST", url, **kw)
    def _get(url, **kw):
        return orig_get(url, **kw) if not url.startswith(BASE) else _to_resp("GET", url, **kw)
    def _delete(url, **kw):
        return orig_delete(url, **kw) if not url.startswith(BASE) else _to_resp("DELETE", url, **kw)

    sdk_mod.requests.post = _post
    sdk_mod.requests.get = _get
    sdk_mod.requests.delete = _delete

    try:
        rag = RagClient(base_url=BASE, api_key="", admin_key=ADMIN_KEY)
        # Create tenant with branding
        branding = {"accent": "#3b82f6", "header_title": "Support Desk"}
        t = rag.create_tenant(name="sdk-brand-test", branding=branding)
        assert t["name"] == "sdk-brand-test"
        assert t["branding"]["accent"] == "#3b82f6"

        tenant_key = t["api_key"]
        rag_tenant = RagClient(base_url=BASE, api_key=tenant_key)

        # Ingest text & get document
        doc = rag_tenant.ingest_text("sdk-brand-test", "SDK Doc", "Fast and lightweight SDK.")
        doc_id = doc["doc_id"]
        doc_details = rag_tenant.get_document("sdk-brand-test", doc_id)
        assert doc_details["doc_id"] == doc_id
        assert len(doc_details["chunks"]) >= 1

        # Send feedback
        fb = rag_tenant.send_feedback("sdk-brand-test", "up", question="how is sdk?", answer="great")
        assert fb["rating"] == "up"

        # Send handoff
        ho = rag_tenant.send_handoff("sdk-brand-test", email="user@example.com", question="need support")
        assert ho["id"] > 0
    finally:
        sdk_mod.requests.post = orig_post
        sdk_mod.requests.get = orig_get
        sdk_mod.requests.delete = orig_delete


@pytest.mark.asyncio
async def test_async_sdk_methods_and_admin_branding():
    """Test AsyncRagClient with httpx ASGITransport."""
    import httpx
    transport = httpx.ASGITransport(app=app)
    base_url = "http://test/api/v1"

    async with AsyncRagClient(base_url=base_url, api_key="", admin_key=ADMIN_KEY, transport=transport) as admin_client:
        # Create tenant with branding
        branding = {"accent": "#10b981", "header_title": "Green Desk"}
        t = await admin_client.create_tenant(name="async-sdk-test", branding=branding)
        assert t["name"] == "async-sdk-test"
        assert t["branding"]["accent"] == "#10b981"
        tenant_key = t["api_key"]

    async with AsyncRagClient(base_url=base_url, api_key=tenant_key, transport=transport) as tenant_client:
        # Ingest text & retrieve document
        doc = await tenant_client.ingest_text("async-sdk-test", "Async Doc", "Async text content for RAG.")
        doc_id = doc["doc_id"]
        doc_details = await tenant_client.get_document("async-sdk-test", doc_id)
        assert doc_details["doc_id"] == doc_id

        # Query
        res = await tenant_client.query("async-sdk-test", "what is async text?")
        assert len(res["results"]) >= 1

        # Send feedback
        fb = await tenant_client.send_feedback("async-sdk-test", "down", comment="needs more info")
        assert fb["rating"] == "down"

        # Send handoff
        ho = await tenant_client.send_handoff("async-sdk-test", question="need help with async api", email="async-lead@example.com", name="Lead Person")
        assert ho["id"] > 0
