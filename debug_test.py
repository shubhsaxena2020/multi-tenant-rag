import json
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
V = "/api/v1"

# Step 1: Create tenant
headers = {"Admin-Key": "test-admin-key-for-tests"}
r = client.post(f"{V}/tenants", json={"name": "acme", "plan": "standard"}, headers=headers)
print(f"Create tenant: status={r.status_code}")
if r.status_code == 201:
    tenant = r.json()
    print(f"  tenant keys: {list(tenant.keys())}")
    api_key = tenant.get("api_key", "")
    print(f"  api_key: {api_key}")
    
    # Step 2: Get auth header
    auth_headers = {"Authorization": f"Bearer {api_key}"}
    
    # Step 3: Get documents
    print(f"\nGET {V}/acme/documents")
    resp = client.get(f"{V}/acme/documents", headers=auth_headers)
    print(f"  Status: {resp.status_code}")
    if resp.status_code < 500:
        print(f"  JSON: {json.dumps(resp.json(), indent=2)[:500]}")
    else:
        print(f"  Response: {resp.text}")