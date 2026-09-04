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
for l in lines[:30]:
    print(l)