from fastapi.testclient import TestClient
import os
os.environ['QDRANT_URL'] = ':memory:'
os.environ.pop('REDIS_URL', None)
os.environ['ADMIN_API_KEY'] = 'test-admin-key-for-tests'
os.environ['USE_REAL_EMBEDDER'] = '0'
os.environ['USE_REAL_RERANKER'] = '0'
os.environ['MASTER_ENCRYPTION_KEY'] = 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'

from app.main import app
from tests.test_adversarial_flows import _create_tenant, V, ADMIN_HEADERS

c = TestClient(app)

t = _create_tenant(client=c, name='adv-theme')
print('Tenant created:', t)

# Now try the branding patch
malicious_branding = {
    "primary_color": "red; background: url(javascript:alert(1));",
    "title": "<script>alert('xss')</script>Support Bot",
    "logo_url": "javascript:alert('xss')",
    "accent_color": "#ff0000",
}

# Try the patch endpoint
r = c.patch(f'{V}/adv-theme/branding', headers=ADMIN_HEADERS, json={'branding': malicious_branding})
print('PATCH status:', r.status_code)
print('PATCH response:', r.text)

# Verify sanitized output
branding = r.json().get('branding', {})
print('Branding title:', repr(branding.get('title', '')))
print('Has script tag:', '<script>' in branding.get('title', ''))
print('Has javascript: in logo_url:', 'javascript:' in branding.get('logo_url', ''))
print('Has javascript: in primary_color:', 'javascript:' in branding.get('primary_color', ''))