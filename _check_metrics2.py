#!/usr/bin/env python3
import os
os.environ['ADMIN_API_KEY'] = 'test-admin-key-for-tests'
from starlette.testclient import TestClient
from app.main import app

client = TestClient(app)
r = client.get('/metrics', headers={'Admin-Key': 'test-admin-key-for-tests'})
body = r.text
lines = [l for l in body.splitlines() if l.strip()]
print('Metric lines returned:', len(lines))
for l in lines[:40]:
    print(l)
# Check for specific metrics
print()
for target in ['rag_no_answer_total', 'rag_no_response_total', 'rag_release_incidents_total', 'rag_faithfulness']:
    found = any(target in l for l in lines)
    print(f'  {target}: {"FOUND" if found else "MISSING"}')