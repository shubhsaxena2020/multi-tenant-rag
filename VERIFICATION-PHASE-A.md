# VERIFICATION-PHASE-A.md

Independent adversarial re-verification of the RAG platform's Phase A security work:
**CORS allowlist (deny-by-default, allowlist-only)** + **publishable (`pk_`) / secret (`rk_`) key-tier split**.

Verified by: agent-6 (independent second-reviewer pass, same shared tmux session as the
orchestrator window 0, operating strictly out-of-band — no files in the main working tree
were modified except transient copies; no panes of agent-1..agent-5 were touched).

---

## 1. What I actually did (not a re-read)

### 1a. Pulled latest from git
`git fetch origin` moved `origin/master` to `916cea1`. Local `master` (v10.4, window 0's WIP)
is **diverged** from origin (local ahead 7 / behind 34) and the `gh` git-credential helper is
broken on this host. I did **not** force a merge/rebase that could clobber window 0's working
tree. Instead I verified the canonical "latest" Phase A code in an **isolated git worktree**
(`/tmp/latest` = `origin/master`, commit `916cea1`) plus a read-only re-confirm against the
local working tree. No working-tree file was modified.

### 1b. Confirmed the baseline is genuinely green
- `origin/master` full suite: **76 passed, 2 skipped** (matches its commit message).
- Isolation/CORS/key-tier subset: **14 passed**.

### 1c. Enumerated EVERY route and its auth dependency (the real gap analysis)
Parsed `app/main.py` for all route decorators + their `Depends(...)` clauses.

| Surface | Auth requirement | Uses key tier? |
|---|---|---|
| `/tenants` POST/GET/DELETE | `require_admin` | admin only |
| `/metrics`, `/audit`, `/audit/verify`, `/api/v1/openapi.json`, `/api/v1/docs` | `require_admin` | admin only (fail-closed) |
| All writes: `/documents`, `/ingest/*`, `/jobs` (+DELETE), `/documents/{id}` DELETE, `/keys` (POST/GET/DELETE prefix), `/keys/publishable`, `/eval/*` | `require_secret_key` | **secret `rk_` only** |
| `/query`, `/query/stream` (SSE) | `TenantDep` (valid key, read) | both `pk_` and `rk_` |
| `/health*`, `/widget.js`, `/widget.html` | public (no key) | n/a |

The SSE `/query/stream` route lives on the **root app**, not the `v1` sub-app, so a naive
scan would miss it — I explicitly checked it and confirmed it requires `TenantDep` (valid key)
but no `require_secret_key`, consistent with `/query` (read-only). No write surface is
ungated. **Every** state-changing route carries `require_secret_key` or `require_admin`.

### 1d. Ran the service LIVE and attacked it over real HTTP
Started the service in **in-memory mode on port 8123** (isolated from any window 0 service)
with `ALLOWED_EMBED_ORIGINS=["https://app.client.com"]` and a test admin key. Then hammered it:

**CORS (deny-by-default allowlist):**
- Allowed origin → `Access-Control-Allow-Origin: https://app.client.com` echoed EXACTLY.
- Disallowed origin (`https://evil.example.com`) → NO `Access-Control-Allow-Origin` header.
- Disallowed-origin preflight → **403** (rejected, not silently passed).
- Confirmed earlier (in-suite) subdomain-prefix, scheme, case, `null`, and literal-`*` attacks
  are all rejected; credentials are never enabled; preflight returns the FIXED method/header
  set, never attacker-requested values.

**Publishable key (`pk_`) vs EVERY secret/write/admin/rotate/revoke/delete/eval/stream endpoint:**
- 15/15 secret-write routes returned **403** with a publishable key (documents, ingest/url,
  ingest/text, ingest/jobs, jobs DELETE, documents DELETE, keys POST/GET/DELETE, keys/publishable,
  eval/set, eval/run, eval/quality, eval/runs, eval/golden/auto).
- Publishable key CAN read: `/query` and `/query/stream` → **200** (by design).
- Publishable key as `Admin-Key` on `/audit`, `/audit/verify`, `/metrics`,
  `/api/v1/openapi.json` → **403** (fail-closed). Without any key → **403** too.

**Cross-tenant isolation with a publishable key (highest-risk area):**
- A's publishable key used against B's namespace path (`/api/v1/{B}/query`) resolved to
  **A's own tenant** (path segment untrusted) and returned A's data — never B's. A write by
  A's publishable key against B's namespace → **403**. No cross-tenant data exposure.

**Key spoof / tier source:**
- Tier is resolved from the DB `kind` column via `get_key_kind()`, NOT from the `rk_`/`pk_`
  prefix. A key literally beginning `rk_` but stored as `kind=publishable` is treated as
  read-only (verified in the prior in-suite adversarial run). A client cannot escalate tier
  by faking the prefix.

**Auth robustness:**
- No `Authorization` header → **401**. Invalid key → **401**.

---

## 2. Results

All checks PASS against the running service and the committed suite. **No reproducible
security gap was found in the Phase A CORS + key-tier surface.**

The 5 "FAIL" lines produced by my first attack script were **test-harness bugs in the script
itself**, not code defects — each was individually disproven with raw `curl`:
- `/audit` etc. returning 404 was a URL-builder bug; real curl shows 403 (no key) / 403
  (publishable as Admin-Key) / 200 (real admin).
- The "leak" on B's path was A's OWN data (correct server-side tenant derivation), identical
  to querying A's own path; B's data never appeared.

---

## 3. Notes for the orchestrator

1. **Git divergence**: local `master` (window 0's v10.4 WIP) and `origin/master` (the
   re-implemented Phase A at `916cea1`) have diverged. They implement the same Phase A
   controls with the same design; I verified BOTH. Before merging, decide which lineage is
   canonical — a plain `git pull` will not fast-forward (diverged branches + broken `gh`
   credential helper on this host). The working tree was also transiently un-importable
   while window 0 was mid-edit; it settled and imports cleanly now.
2. **Fix already in place** (good): `/query/stream` enforces `rate_limit` (P0 fix) and
   `TenantDep` — it is not an unauthenticated or unthrottled write surface.
3. **No code changes were made by this verification.** The Phase A work is sound; no fix or
   regression test was required because nothing was broken.

---

## 4. Reproduction

```
# baseline (origin/master in isolated worktree, no working-tree mutation)
git fetch origin
git worktree add /tmp/latest origin/master
cd /tmp/latest && PYTHONPATH=/tmp/latest .venv/bin/python -m pytest tests/ -q
# live attack: start on a private port, run attack_live.py (CORS + every-route key-tier)
```

Verified: 2026-08-29 · agent-6 · scope: Phase A CORS + publishable/secret key tiers only.
