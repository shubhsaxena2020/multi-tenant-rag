import json
from app.main import app
from app.config import get_settings
from fastapi.testclient import TestClient

s = get_settings()
c = TestClient(app, headers={'Admin-Key': s.admin_api_key})

# Run alignment checks
print('=== METRICS/RUNBOOK ALIGNMENT PASS ===')
print()

# 1. /admin/summary metrics
r = c.get('/admin/summary')
d = r.json()
print('1. /admin/summary metrics:')
print('   tenant_count:', d.get('tenant_count'))
print('   totals:', json.dumps(d.get('totals'), indent=4))
print()

# 2. /admin/token-usage metrics
r2 = c.get('/admin/token-usage')
t = r2.json()
print('2. /admin/token-usage metrics:')
print('   tenant_count:', t.get('tenant_count'))
print('   calls:', t.get('calls'))
print('   total_tokens:', t.get('total_tokens'))
print('   cost_usd:', t.get('cost_usd'))
print('   cost_basis:', t.get('cost_basis'))
print()

# 3. Contract verification
match = d.get('tenant_count') == t.get('tenant_count')
print('3. tenant_count contract: {} {}'.format('PASS' if match else 'FAIL', '✓' if match else '✗'))
print()

# 4. Runbook check: totals should have all expected keys
totals = d.get('totals', {})
expected_keys = ['queries', 'docs_ingested', 'chunks_ingested', 'eval_runs',
                 'feedback_up', 'feedback_down', 'leads', 'knowledge_gaps']
missing = [k for k in expected_keys if k not in totals]
extra = [k for k in totals.keys() if k not in expected_keys]
print('4. Runbook alignment - totals keys match: {} {}'.format(
    'PASS' if not missing and not extra else 'CHECK', '✓' if not missing and not extra else '✗'))
if missing:
    print('   Missing from runbook:', missing)
if extra:
    print('   Extra keys:', extra)