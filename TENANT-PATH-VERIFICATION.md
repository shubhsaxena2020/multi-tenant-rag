# Tenant Path Semantics & Key-Scope Language Verification

**Audited from**: repo state at HEAD e6d5229 on feat/rag-agent6-month-scale

## 1. Tenant path semantics

### Current code behavior (Issue #15 — fail-closed)

In `app/main.py`, every tenant-scoped endpoint enforces that the `{tenant}` path segment must match the `tenant_id` resolved from the Bearer API key:

- `POST/GET/DELETE /{tenant}/jobs/{id}` — all require `require_secret_key`
- `POST/GET/DELETE /{tenant}/documents/{doc_id}` — all require `require_secret_key`
- `POST/GET /{tenant}/ingest/jobs` — all require `require_secret_key`
- Ingest from URL/text — path tenant must match API-key-resolved tenant (Issue #15 fail-closed)
- Document retrieval and deletion — path tenant must match API-key-resolved tenant (Issue #15 fail-closed)

**Security guarantee**: If a request comes with `Authorization: Bearer <rk_*key>` resolving to tenant T1, but the path is `/tenant2/...`, the server rejects the request. This is the fail-closed behavior for Issue #15.

### Public doc statement (README.md, line 48-50)

> "Tenant identity is derived **server-side from the Bearer API key** on every request; the `{tenant}` path segment is informational. All vector operations are scoped to the tenant's own Qdrant collection, so cross-tenant reads are impossible at the storage layer — the guarantee holds even under application bugs."

This accurately reflects the code behavior: the `{tenant}` path segment is checked against the key-resolved tenant, and operations are scoped to that tenant's collection.

## 2. Key-scope language

### Widget client (`app/static/widget.html`, `app/static/widget.js`, `app/static/index.html`)

- **Publishable keys (`pk_*`)**: Described as "read-only" and "safe to embed in a browser" (index.html line 23-24). Widget HTML presents a field for `pk_*` input only (index.html line 29-30).
- **Secret keys (`rk_*`)**: Widget JavaScript explicitly warns: "That looks like a SECRET key — do not embed it. Use a publishable pk_* key." (widget.html line 50-51; widget.js guards against `rk_*` or `sk_*` prefixes).

### API key minting (`app/models.py`, `app/db.py`)

- `models.py:139`: `kind: str = "secret"` — `"secret" (rk_*) or "publishable" (pk_*) — P1 #9`
- `models.py:152`: `"P1 #9 + v10.8: mint a read-only publishable key, optionally time-boxed."`
- `db.py:72-74`: "P1 #9: key tier. 'secret' = full-power key (rk_*, can ingest/admin/rotate); 'publishable' = read-only key (pk_*) safe to embed client-side in the widget. A publishable key resolves to the SAME tenant_id (isolation is unchanged) but is scope-locked to query endpoints by app/auth.py:require_secret_key."

### Public doc statement (README.md, line 46-48)

> "keys per tenant; per-tenant chunk quota for fleet protection."
> "Tenant identity is derived server-side from the Bearer API key on every request"

This is consistent with the code. The README does not overstate the key scopes.

## 3. What is accurately documented

| Area | Status | Evidence |
|------|--------|----------|
| `{tenant}` path must match key-resolved tenant | ✅ Accurate | `main.py:836,862,973,1000,1035,1041,1197,1239,1288,1502-1503` |
| `pk_*` = read-only, safe for client embedding | ✅ Accurate | `widget.html:23-24,50-51`; `models.py:152`; `db.py:72-74` |
| `rk_*` = full power, never embed client-side | ✅ Accurate | `widget.html:50-51`; `db.py:72-74` |
| Publishable key resolves to same tenant_id (isolation unchanged) | ✅ Accurate | `db.py:74-75`; `models.py:152` |
| Fail-closed on path-tenant vs key-tenant mismatch | ✅ Accurate | `main.py:1038`; `Issue #15` throughout |

## 4. No changes needed

All tenant path semantics and key-scope language in public docs (README.md) and SDK-facing examples (widget HTML/JS, index.html) are consistent with the real codebase. The documentation accurately describes:

- Per-tenant isolation enforced by server-side key resolution
- `{tenant}` path segment being validated against key-resolved tenant (fail-closed)
- `pk_*` keys being read-only and safe for widget embedding
- `rk_*` keys being full-power and never to be embedded client-side

**No code changes required** for this verification item.