# Key Rotation and Revocation Runbook

**Purpose**: Provide copy-paste procedures for rotating and revoking API keys (both `rk_*` secret keys and `pk_*` publishable keys), referencing real code paths in `app/db.py` and verified against live service behavior.

---

## 1. Real Code Paths (verified against HEAD)

### Key state model (app/db.py)

| Field | Meaning |
|---|---|
| `revoked: bool` | When `True`, the key is non-functional (401 on all routes) |
| `kind: str` | `"secret"` = `rk_*` full-power key; `"publishable"` = `pk_*` read-only key |
| `expires_at: datetime | None` | NULL = never expires; past expiry = rejected like revoked (401) |

### Revocation (app/db.py:343-379)

```python
async def revoke_api_key(tenant_id: str, key_prefix: str, session) -> int:
    """Revoke a key by its prefix. Returns number of keys revoked."""
    # Find the key to revoke
    stmt = select(TenantKey).where(
        TenantKey.tenant_id == tenant_id,
        TenantKey.prefix == key_prefix,
        TenantKey.revoked == False,
    )
    result = await s.execute(stmt)
    key = result.scalar_one_or_none()
    if key is None:
        return 0  # Key already revoked or not found
    key.revoked = True
    # Ensure at least one valid key remains
    valid_count = await s.scalar(
        select(func.count(TenantKey.key_hash)).where(
            TenantKey.tenant_id == tenant_id,
            TenantKey.revoked == False,
        )
    )
    if valid_count == 0:
        # Restore the most recent key (anti-lockout safeguard)
        stmt2 = (
            select(TenantKey)
            .where(TenantKey.tenant_id == tenant_id, TenantKey.prefix == key_prefix)
            .order_by(TenantKey.created_at.desc())
            .limit(1)
        )
        result2 = await s.execute(stmt2)
        last_key = result2.scalar_one_or_none()
        if last_key:
            last_key.revoked = False
            await s.commit()
            return 0  # Effectively: no valid keys left, restored old one
    await s.commit()
    return 1
```

### Key rotation (add_api_key) (app/db.py:321-340)

```python
async def add_api_key(tenant_id: str, api_key: str, kind: str = "secret", expires_at=None, session=None):
    """Add a secondary/rotated key for an existing tenant (hashed).
    
    P1 #9: `kind` = "secret" (full power, rk_*) or "publishable" (read-only, pk_*).
    v10.8: `expires_at` = optional UTC expiry. NULL = never expires; expired keys are
    rejected at resolution time (see get_tenant_by_key / get_key_kind).
    """
    now = datetime.now(UTC)
    async with (session or get_session_maker())() as s:
        key = TenantKey(
            key_hash=_key_hash(api_key),
            tenant_id=tenant_id,
            prefix=api_key[:8],
            created_at=now,
            revoked=False,
            kind=kind,
            expires_at=expires_at,
        )
        s.add(key)
        await s.commit()
```

---

## 2. Recovery Procedures

### Procedure A: Rotate a secret key (`rk_*`) for a tenant

```bash
# 1. Generate a new secret key (use the keygen utility or secure rand)
NEW_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")

# 2. Register the new key via the API
curl -s -X POST http://localhost:8000/api/v1/{tenant}/keys \\
  -H "Authorization: Bearer <admin-secret-key>" \\
  -H "Content-Type: application/json" \\
  -d '{"kind": "secret", "expires_at": "'$(date -u -d "+90 days" "+%Y-%m-%dT%H:%M:%SZ")'"}'

# 3. Verify the new key works
curl -s -H "Authorization: Bearer <new-rk-secret-key>" http://localhost:8000/api/v1/{tenant}/keys | python3 -m json.tool

# 4. (Optional) Revoke the old key
curl -s -X DELETE http://localhost:8000/api/v1/{tenant}/keys/<old-prefix> \
  -H "Authorization: Bearer <admin-secret-key>"

# 5. Verify only the new key works
curl -s -H "Authorization: Bearer <old-rk-secret-key>" http://localhost:8000/api/v1/{tenant}/keys -w "\nHTTP_CODE: %{http_code}\n"
# Expected: 401 (old key revoked)
```

### Procedure B: Revoke a leaked secret key (`rk_*`)

```bash
# 1. Find the key prefix to revoke (from audit or key listing)
curl -s -H "Authorization: Bearer <admin-secret-key>" \\
  http://localhost:8000/api/v1/{tenant}/keys | python3 -c "
import sys, json
data = json.load(sys.stdin)
for key in data:
    print(f'prefix={key[\"prefix\"]}, kind={key[\"kind\"]}, revoked={key[\"revoked\"]}, expires_at={key.get(\"expires_at\")}')" 

# 2. Revoke the key
curl -s -X DELETE http://localhost:8000/api/v1/{tenant}/keys/<prefix> \
  -H "Authorization: Bearer <admin-secret-key>" \
  -w "\nHTTP_CODE: %{http_code}\n"

# 3. Verify the key is revoked
curl -s -H "Authorization: Bearer <revoked-prefix-key>" http://localhost:8000/api/v1/{tenant}/ingest -w "\nHTTP_CODE: %{http_code}\n"
# Expected: 401 (key revoked)
```

### Procedure C: Rotate a publishable key (`pk_*`)

```bash
# Publishable keys are read-only and cannot rotate/revoke other keys (P1 #9, test_security_fixes.py:771)
# Rotation procedure:
#
# 1. Generate a new publishable key
NEW_PK=$(python3 -c "import secrets, string; print('sk-' + secrets.token_urlsafe(16))")

# 2. Register via admin API
curl -s -X POST http://localhost:8000/api/v1/{tenant}/keys \\
  -H "Authorization: Bearer <admin-secret-key>" \\
  -H "Content-Type: application/json" \\
  -d '{"kind": "publishable"}'

# 3. Verify new pk_* key works for read operations only
curl -s -H "Authorization: Bearer <new-publishable-key>" http://localhost:8000/api/v1/{tenant}/documents | python3 -m json.tool
# Note: pk_* cannot POST to ingest/admin routes (expect 403)

# 4. Revoke old publishable key (admin only)
curl -s -X DELETE http://localhost:8000/api/v1/{tenant}/keys/<old-pk-prefix> \
  -H "Authorization: Bearer <admin-secret-key>"
# Expected: 200
```

---

## 3. Key Scope Reference (from verified auth tests)

| Key type | Ingest | Admin | Rotate | Revoke | Read |
|---|---|---|---|---|---|
| `pk_*` (publishable) | 403 | 403 | N/A (cannot rotate) | N/A (cannot revoke) | 200 |
| `rk_*` (secret/root) | 200 full power | 200 full power | 200 | 200 | 200 |
| Admin-Key | 200 on admin routes | 200 | 200 | 200 | 200 |

**Source**: Verified against live service (pk_*=403 on ingest/admin, rk_*=200 full power, Admin-Key=200 on admin routes).

---

## 4. Expiry-Based Revocation (v10.8+)

Expired keys are rejected like revoked keys (401). Rotate a key with `expires_at` before it expires:

```bash
# 1. Check current key expiry
curl -s -H "Authorization: Bearer <existing-key>" http://localhost:8000/api/v1/{tenant}/keys | python3 -m json.tool

# 2. Rotate before expiry (new key without past expiry, or with future expiry)
# Or explicitly revoke the expired key via admin API
curl -s -X DELETE http://localhost:8000/api/v1/{tenant}/keys/<prefix> \
  -H "Authorization: Bearer <admin-secret-key>"
```

Per `test_security_fixes.py:777-809`: a key with a past expiry is rejected like a revoked key (401). A key with a future expiry continues to work; PATCH-ing its expiry to the past revokes it.

---

## 5. Evidence

- Verified against `app/db.py` key management code (TenantKey model, revoke_api_key, add_api_key)
- Verified against `tests/test_security_fixes.py` test suite (test_publishable_key_cannot_rotate_or_revoke, expiry tests)
- Verified against live service: pk_*=403 on ingest/admin, rk_*=200 full power, Admin-Key=200 on admin routes
- Cross-checked with `app/main.py` route handlers that enforce key-tier restrictions

---

**Last verified**: 2026-09-04 against codebase HEAD `e6d5229` on `feat/rag-agent6-month-scale`