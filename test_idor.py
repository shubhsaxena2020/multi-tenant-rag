import os

os.environ["ADMIN_API_KEY"] = "test-admin-key-for-tests"
os.environ["MASTER_ENCRYPTION_KEY"] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
os.environ["QDRANT_URL"] = ":memory:"
os.environ.pop("REDIS_URL", None)
os.environ["USE_REAL_EMBEDDER"] = "0"
os.environ["USE_REAL_RERANKER"] = "0"

from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

# Create two tenants
r1 = client.post("/api/v1/tenants", json={"name": "idor-a", "plan": "standard"}, headers={"Admin-Key": os.environ["ADMIN_API_KEY"]})
r2 = client.post("/api/v1/tenants", json={"name": "idor-b", "plan": "standard"}, headers={"Admin-Key": os.environ["ADMIN_API_KEY"]})
t1 = r1.json(); t2 = r2.json()
a1 = {"Authorization": f"Bearer {t1['api_key']}"}
a2 = {"Authorization": f"Bearer {t2['api_key']}"}

results = []

# 1. GET document list with wrong tenant key
r = client.get(f"/api/v1/{t2['tenant_id']}/documents", headers=a1)
results.append(f"1. GET /{{t2}}/documents with t1 key: {r.status_code} (expect 404)")

# 2. GET single document with wrong tenant key
ingest_r = client.post(f"/api/v1/{t2['tenant_id']}/documents", headers=a2, json={"title": "secret", "content": "B secret docs"})
doc_id = ingest_r.json()["doc_id"]
r = client.get(f"/api/v1/{t2['tenant_id']}/documents/{doc_id}", headers=a1)
results.append(f"2. GET /{{t2}}/documents/{{doc}} with t1 key: {r.status_code} (expect 404)")

# 3. DELETE document with wrong tenant key
r = client.delete(f"/api/v1/{t2['tenant_id']}/documents/{doc_id}", headers=a1)
results.append(f"3. DELETE /{{t2}}/documents/{{doc}} with t1 key: {r.status_code} (expect 404)")

# 4. Query across tenants (Pool mode: returns key's own data)
r = client.post(f"/api/v1/{t2['tenant_id']}/query", headers=a1, json={"question": "test", "top_k": 1, "generate": False})
results.append(f"4. POST /{{t2}}/query with t1 key: {r.status_code} (Pool: 200 with t1 data)")

# 5. Widget config with wrong tenant key
r = client.get(f"/api/v1/{t2['tenant_id']}/widget/config", headers=a1)
results.append(f"5. GET /{{t2}}/widget/config with t1 key: {r.status_code} (expect 403)")

# 6. Session history with wrong tenant key
r = client.get(f"/api/v1/{t2['tenant_id']}/session/test-sess", headers=a1)
results.append(f"6. GET /{{t2}}/session/{{sid}} with t1 key: {r.status_code} (expect 403 per ISSUE #15)")

# 7. List documents with wrong tenant key
r = client.get(f"/api/v1/{t2['tenant_id']}/documents", headers=a1)
results.append(f"7. GET /{{t2}}/documents with t1 key: {r.status_code} (expect 404)")

# 8. Admin routes with user key
r = client.get("/admin/usage/test", headers=a1)
results.append(f"8. GET /admin/usage/test with user key: {r.status_code} (expect 403)")

# 9. Key rotation with wrong tenant key
r = client.post(f"/api/v1/{t2['tenant_id']}/keys", headers=a1, json={})
results.append(f"9. POST /{{t2}}/keys with t1 key: {r.status_code} (expect 404)")

# 10. Publishable key on query-only routes
r_pk = client.post(f"/api/v1/{t1['tenant_id']}/keys/publishable", headers=a1, json={})
pk_key = r_pk.json()["api_key"]
pub_auth = {"Authorization": f"Bearer {pk_key}"}
r = client.post(f"/api/v1/{t1['tenant_id']}/query", headers=pub_auth, json={"question": "test", "top_k": 1, "generate": False})
results.append(f"10. POST /{{t1}}/query with pk key: {r.status_code} (expect 200)")

r_b1 = client.post(f"/api/v1/{t1['tenant_id']}/documents", headers=pub_auth, json={"title": "Hack", "content": "Inject"})
results.append(f"11. POST /{{t1}}/documents with pk key: {r_b1.status_code} (expect 403)")

# 11. Ingest URL with wrong tenant key
r = client.post(f"/api/v1/{t2['tenant_id']}/ingest/url", headers=a1, json={"url": "https://example.com", "title": "test"})
results.append(f"12. POST /{{t2}}/ingest/url with t1 key: {r.status_code} (expect 404)")

# 12. Ingest text with wrong tenant key
r = client.post(f"/api/v1/{t2['tenant_id']}/ingest/text", headers=a1, json={"title": "test", "text": "hello"})
results.append(f"13. POST /{{t2}}/ingest/text with t1 key: {r.status_code} (expect 404)")

# 12. Publishable key delete doc
client.post(f"/api/v1/{t1['tenant_id']}/documents", headers=a1, json={"title": "todel", "content": "to delete"})
r = client.delete(f"/api/v1/{t1['tenant_id']}/documents/todel", headers=pub_auth)
results.append(f"14. DELETE /{{t1}}/documents/{{doc}} with pk key: {r.status_code} (expect 403)")

# 13. List keys with wrong tenant key
r = client.get(f"/api/v1/{t2['tenant_id']}/keys", headers=a1)
results.append(f"15. GET /{{t2}}/keys with t1 key: {r.status_code} (expect 404)")

# 13. Create secret key with wrong tenant key
r = client.post(f"/api/v1/{t2['tenant_id']}/keys/secret", headers=a1, json={})
results.append(f"16. POST /{{t2}}/keys/secret with t1 key: {r.status_code} (expect 404)")

print("\n=== IDOR / Cross-tenant Authorization Results ===")
for r in results:
    print(r)

print("\n=== Summary ===")
# Count 404s and 403s expected
expected_404 = 9  # routes that should fail-closed
expected_403 = 3  # routes that should return 403 (widget config, session, admin)
four_oh_four = sum(1 for r in results if "404" in r)
four_oh_three = sum(1 for r in results if "403" in r)
print(f"404 responses: {four_oh_four}/{expected_404} (expected {expected_404})")
print(f"403 responses: {four_oh_three}/{expected_3} (expected {expected_403})")
SCRIPT
