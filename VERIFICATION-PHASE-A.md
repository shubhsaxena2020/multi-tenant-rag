# VERIFICATION-PHASE-A.md

Independent adversarial re-verification of the RAG platform's Phase A security work:
**CORS allowlist (deny-by-default, allowlist-only)** + **publishable (`pk_`) / secret (`rk_`) key-tier split**
+ cross-tenant isolation (the highest-regression-risk area).

Verified by: agent-6 (independent second-reviewer pass, same shared tmux session as the
orchestrator window 0, operating strictly out-of-band — no files in the other agents' working
trees were modified; no panes of agent-1..agent-5 were touched).

Scope of THIS pass (2026-08-29, second/deeper review):
1. Enumerate EVERY route in `app/main.py` and check each one's auth dependency explicitly
   (not just the routes with existing tests).
2. Re-run the full cross-tenant isolation suite as a final sanity check.
3. Live-adversarial confirmation (real HTTP against a running instance) for CORS + key tier.

---

## 0. Method / isolation posture

- Repo `/home/ubuntu/rag-service`, HEAD at verification time `dfe72e8` (orchestrator's
  PHASE F commit). `git diff origin/master -- app/auth.py app/main.py` shows the local tree
  only *adds* the key-tier logic (incl. `require_secret_or_publishable`) on top of the
  `origin/master` (`916cea1`) lineage — the CORS middleware block and tier checks are
  byte-identical across both lineages.
- Live service run in an ISOLATED sandbox: `QDRANT_URL=:memory:`, a throwaway SQLite
  (`/tmp/verify_rag/*.db`), fake `ADMIN_API_KEY`/`MASTER_ENCRYPTION_KEY`, explicit
  `ALLOWED_EMBED_ORIGINS`, rate limits set to 0 (so a 429 can never mask a 403), bound to
  `127.0.0.1:8123`. The production `.env` was NOT loaded. Service killed and temp removed
  after testing; **no artifacts left in the repo working tree.**
- Two tenants (A, B) were seeded with DISTINCT real content so cross-tenant effects are
  detectable by sentinel tokens, not by assumption.

---

## 1. Route enumeration — did any endpoint miss the key-tier restriction?

Parsed `app/main.py` with an AST pass over `app` + `v1` (the only two router objects; the
`v1` sub-app is mounted at `/api/v1`; no `add_api_route`/`include_router`/extra mounts).
**46 routes enumerated.** Each route's full `Depends(...)` set was extracted from both the
decorator and the function signature. Result:

- **Every state-changing route** (`POST/PUT/DELETE` on documents, ingest, jobs, sitemap,
  keys, eval, config, lead, knowledge-gaps, analytics) carries `require_secret_key`.
- **Every admin/privileged route** (`/tenants`, `/api/v1/admin/console`, `/metrics`,
  `/audit`, `/audit/verify`, `/api/v1/openapi.json`, `/api/v1/docs`) carries `require_admin`.
- **Only `/query` and `/api/v1/{tenant}/query/stream` are gated by `TenantDep` alone** —
  these are read-only by design, and a publishable key legitimately needs to call them.
- Public (no auth): `/health*`, `/widget.js`, `/widget.html` only.
- `require_secret_key` / `require_admin` / `require_secret_or_publishable` all read the
  API key **only** from the `Authorization: *** header (never the path), so the tier can
  only be satisfied by presenting a valid Bearer key of the right `kind`.

**No endpoint was missed.** There is no write/admin route reachable by a publishable key.
(AST enumeration script: `/tmp/enum_routes.py` — re-runnable.)

Note on the one `TenantDep`-only WRITE-looking route: `POST /query` and `/query/stream` are
read paths (they retrieve/stream answers; they do not mutate tenant data), so `TenantDep`
alone is correct. This matches the design intent and is not a gap.

---

## 2. Live key-tier confirmation (publishable key vs every privileged route)

A real publishable key (`pk_`) was fired at **all 35** admin/ingest/delete/rotate/write/
secret-gated-GET routes with valid-shaped request bodies (so a 403 can only come from the
tier check, not a 422 that would mask it). **35/35 returned 403.** The only routes a `pk_`
is permitted on: `/query` (200), `/query/stream` (200), `/feedback` (201 —
`require_secret_or_publishable`, benign thumbs rating). A server-minted key starting with
`pk_` (stored `kind=publishable`) cannot escalate to a write even if a client fakes the
prefix — tier is resolved from the DB `kind` column via `get_key_kind()`.

---

## 3. CORS — deny-by-default allowlist (raw header evidence)

- Allowed origin → `Access-Control-Allow-Origin` echoed EXACTLY (never `*`, never a
  client-supplied value).
- Disallowed origin (`https://evil.example.com`) → NO `ACAO` header.
- `Access-Control-Allow-Credentials` is NEVER set (auth is header-based, not cookie-based).
- Disallowed-origin preflight → `403 Forbidden` (hard reject; browser never sends the real
  request). Allowed-origin preflight → 200 with the exact origin echoed.
- Realistic attack: a valid victim `pk_` sent from a disallowed origin returns 200 but with
  NO `ACAO` → the browser blocks the response body from the attacker's JS.

---

## 4. Cross-tenant isolation — final sanity check (25/25 PASS)

Re-run with two tenants and distinct sentinels. Confirmed:

- Each key's own-path query returns ONLY its own data; never the other tenant's.
- A key for tenant X used against tenant Y's **namespace path** resolves to X (path segment
  untrusted) and returns X's data; Y's data is never exposed. Holds for both secret and
  publishable keys, and for `/query/stream`.
- **Cross-namespace WRITE**: a publishable key writing/deleting against another tenant's
  path → `403`. A **secret** key writing/deleting against another tenant's path *may*
  return 201/200, but empirically the write lands in the **key's OWN** collection — the
  `{tenant}` path segment is NOT trusted as the write destination (every handler passes
  `auth.tenant_id` to the storage layer; verified at source and at runtime). The other
  tenant's documents are provably unchanged. **No cross-tenant read or write is possible.**

### Behavioral note (NOT a vulnerability, flagged for clarity)
A secret key presented against a *different* tenant's path segment currently writes to the
key's **own** tenant (path ignored), returning a success that could mislead a caller about
which namespace they hit. It is safe (no cross-tenant effect) but is a misleading-success
footgun. Recommendation: optionally 400/404 when the path `tenant` segment does not match
`auth.tenant_id`, to make the contract explicit and prevent future regressions where a
handler might start trusting the path. This is a hardening suggestion, not a fix for a gap.

---

## 5. Result

**No reproducible security gap found in the Phase A CORS + publishable/secret key-tier
surface, and cross-tenant isolation holds on both read and write paths.** Enumerated all 46
routes; the key-tier restriction is complete; CORS is deny-by-default; isolation is intact.

Caveat observed out of scope: `GET /api/v1/tenants` with a *real* admin key returns 500
(pre-existing admin-handler defect, unrelated to the tier guarantee, worth its own ticket).

---

## 6. Recommendations
1. Add a committed live adversarial test (`tests/test_adversarial_phaseA.py`) covering the
   enumeration + cross-tenant isolation matrix above (the current committed suite has
   key-tier assertions in `tests/test_security_fixes.py` but no dedicated enumeration/isolation
   adversarial test; a stale `.pyc` suggested one existed but no source file is present).
2. Consider the path/key mismatch hardening noted in §4.

## 7. Reproduction (isolated, non-destructive)
```
mkdir /tmp/verify_rag && cd /tmp/verify_rag
export PYTHONPATH=/home/ubuntu/rag-service QDRANT_URL=:memory: \
  DB_URL=sqlite:////tmp/verify_rag/v.db ADMIN_API_KEY=test \
  MASTER_ENCRYPTION_KEY=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA \
  USE_REAL_EMBEDDER=0 USE_REAL_RERANKER=0 \
  ALLOWED_EMBED_ORIGINS='["https://app.client.com"]' \
  RATE_PER_TENANT_PER_MIN=0 RATE_PER_IP_PER_MIN=0 RATE_INGEST_JOBS_PER_MIN=0
uvicorn app.main:app --host 127.0.0.1 --port 8123 &
python enum_routes.py         # AST enumeration of all routes + tier flags
python phase_a_verify.py      # CORS + key-tier + isolation matrix
python isolation_final.py     # 25/25 cross-tenant isolation suite
```

Verified: 2026-08-29 · agent-6 · scope: Phase A CORS + publishable/secret key tiers + cross-tenant isolation.
