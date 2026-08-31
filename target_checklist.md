# Sentinel Target Checklist — rag-service

**Purpose:** Audit scope sign-off for future Sentinel scans. This checklist is reviewed by the operator before any live scan is permitted.

## SSRF Guard Rules (always enforced — no operator sign-off needed)

| Rule | Status | Notes |
|---|---|---|
| Allowed schemes: `http`, `https` only | ✅ Enforced | `javascript:`, `data:`, `ftp:` etc. rejected |
| Allowed ports: `80`, `443` only | ✅ Enforced | Internal/admin ports blocked |
| Loopback/RFC1918/Link-Local blocked | ✅ Enforced | 127.0.0.1, 10.x, 172.16-31, 192.168 blocked |
| Cloud metadata endpoint blocked | ✅ Enforced | `169.254.169.254` blocked |

## CORS Allowlist

| Item | Current | Required for Scan |
|---|---|---|
| `allowed_embed_origins` | `[]` (empty → deny-by-default) | Must be populated with trusted widget origins |
| `allow_credentials` | `false` | Must be `false` until origins are populated |
| Preflight response | 403 for disallowed origins | Maintained |

## ACL Allowlist (enforced at parse time)

| Condition | Result |
|---|---|
| `["*"]` | 422: `"acl must not contain wildcard '*'"` |
| `[]` | 422: `"acl must contain at least one group"` |
| Non-list input | 422: `"acl must be a JSON array of strings"` |
| Non-string elements | 422: `"acl must be a JSON array of strings"` |
| Valid: `["hr", "engineering"]` | Returned as-is |

## Key Tier Enforcement

| Key Type | Operations Allowed | Operations Rejected |
|---|---|---|
| `rk_*` (secret) | Full: ingest, admin, rotate, revoke, delete | — |
| `pk_*` (publishable) | Query only: `POST /{tenant}/query` | 403 on all admin/ingest/rotate/revoke |

## Rate Limits

| Boundary | Config | Behavior |
|---|---|---|
| Per-IP | `RATE_PER_IP_PER_MIN=120` | 429 + `Retry-After` when exceeded |
| Per-tenant | `RATE_PER_TENANT_PER_MIN=600` | 429 + `Retry-After` when exceeded |

## Operator Sign-Off Required Before Live Scan

- [ ] `allowed_embed_origins` populated with trusted widget domains
- [ ] Egress network policy confirms approved outbound destinations
- [ ] SSRF guard rules reviewed and approved (current rules are deny-by-default and hold without further approval)
- [ ] `safe_scope.json` populated with explicit scan targets (currently empty → deny-all)
- [ ] All above items verified in writing by operator

**Next action:** Populate `safe_scope.json` with operator-approved entries and obtain sign-off on `target_checklist.md`.
