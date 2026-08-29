# RAG Service — Design Tradeoffs & Enforcement Principles

Each shipped security/robustness control, mapped to the concrete violation it blocks
and the hardening principle it embodies. Grouped by the CIA property it defends.

## A. SSRF egress guard — app/ingestion/ssrf.py::validate_url_for_egress
- Blocks: non-http(s) schemes (file://, gopher://), empty/missing host, internal hostnames
  (localhost, *.local, *.internal, *.svc), blocked IP ranges (169.254.x, 127.x, 10.x,
  192.168.x, 172.16-31.x, ::1), non-80/443 ports. Two modes: resolve=False (config-time,
  structural only, no DNS) and resolve=True (dispatch-time, full DNS + IP-pin).
- Principle: IMPLICIT DENY on outbound egress + trust-boundary enforcement.
- Class: Server-Side Request Forgery -> metadata theft, internal pivot, port scan. [CONF/INTEG]

## B. HMAC-signed webhooks — X-Webhook-Signature: sha256=<hmac> (webhook.py)
- Blocks: unsigned callbacks, tampered payloads, forged-sender injection.
- Principle: MESSAGE AUTHENTICITY + INTEGRITY (HMAC over raw body). Non-repudiation on egress.
- Class: spoofed-webhook injection, replay/tamper. [INTEG]

## C. Best-effort dispatch — webhook.py never raises; logs webhook_failed / webhook_ssrf_blocked
- Blocks: a webhook outage taking down the core ingest job.
- Principle: GRACEFUL DEGRADATION / critical-path isolation. [AVAIL]

## D. Webhook fired outside the asyncio loop — runner.py::_fire_ingest_webhook from _run
- Blocks: RuntimeError: asyncio.run() cannot be called from a running loop.
- Principle: SAFE CONCURRENCY (correctness-under-async) — the security feature must actually fire.

## E. Per-tenant chunk quota + global default + 429/Retry-After — ingestion/__init__.py::_enforce_quota
- Blocks: one tenant exhausting shared storage/compute (over-quota ingestion).
- Principle: RESOURCE ISOLATION / anti-noisy-neighbor / capacity bounding. [AVAIL]

## F. Rate limiting — per-IP + per-tenant, Redis-backed, 429 + Retry-After — ratelimit.py
- Blocks: request floods, brute-force, single-client abuse.
- Principle: THROTTLING / abuse prevention / fair-access. [AVAIL]

## G. Server-side tenant identity — db.py::get_tenant_by_key (tenant_id from key hash, never path/body)
- Blocks: client-supplied or path-spoofed tenant identity -> cross-tenant read/write.
- Principle: NEVER TRUST CLIENT-SUPPLIED IDENTITY / zero-trust authorization. Structural, not convention.
  [CONF] (the core multitenancy invariant)

## H. Pub/secret key tiers — pk_* read-only, rk_* full; require_secret_key rejects pk_*
- Blocks: a leaked embeddable key performing ingest/delete/rotate/revoke/admin/eval.
- Principle: LEAST PRIVILEGE / capability scoping / privilege separation. [CONF/INTEG]

## I. Key expiry — db.py TenantKey.expires_at; expired keys rejected at resolution like revoked
- Blocks: use of stale/leaked long-lived credentials past validity window.
- Principle: TIME-BOUND CREDENTIALS / credential-lifecycle hygiene. [CONF]

## J. Last-valid-key protection — db.py::revoke_api_key restores most-recent if zero would remain
- Blocks: self-revocation to zero -> permanent lockout.
- Principle: FAIL-SAFE AVAILABILITY / no self-inflicted DoS. [AVAIL]

## K. Audit log — SHA-256 hash-chained, admin-gated GET /audit + GET /audit/verify — audit.py
- Blocks: untraceable admin actions, post-hoc log tampering.
- Principle: ACCOUNTABILITY / NON-REPUDIATION / append-only integrity + least-exposure read. [INTEG/ACCT]

## L. Admin-key fail-closed — admin routes require valid Admin-Key; missing/invalid => deny
- Blocks: unauthorized tenant/key/eval manipulation.
- Principle: DEFAULT DENY / privileged-action gating. [INTEG/CONF]

## M. Secrets hygiene — redacted MASTER_ENCRYPTION_KEY; api_key shown once; raw Admin-Key never
   stored (only server-derived fingerprint)
- Blocks: secret leakage via logs/config/diff.
- Principle: CONFIDENTIALITY / least exposure / secure secret handling. [CONF]

## N. Secure-by-default loopback escape — WEBHOOK_ALLOW_PRIVATE env; production default False
- Blocks: accidentally relaxing SSRF in production via env misconfig.
- Principle: SECURE DEFAULTS / explicit opt-in for risk. Relaxation is env-scoped, never implicit. [CONF]

## O. Non-destructive migration — init_db ALTER COLUMN IF NOT EXISTS (tenants + tenant_keys)
- Blocks: deploy breaking against stale live DB / data loss on upgrade.
- Principle: SAFE DEPLOYMENT / backward compatibility / no-data-loss. [AVAIL/INTEG — operational]

## Through-line
Every constraint is one of:
  1. DEFAULT-DENY trust boundary (SSRF, rate limit, admin gating, server-side identity)
  2. LEAST-PRIVILEGE / blast-radius limiter (key tiers, expiry, quota)
  3. INTEGRITY / ACCOUNTABILITY control (HMAC, audit chain, secrets hygiene)
Standard zero-trust + defense-in-depth shape.

## Open backlog (not yet shipped)
- Horizontal scaling of ingestion workers: in-memory job queue (db.py jobs) is durable but
  single-replica. For N replicas, front with at-least-once queue (Cloud Tasks / RQ / Celery)
  that calls runner._run; requeue_orphaned_jobs() already exists. Need JOB_QUEUE backend setting.
- i18n: detect query lang, pick embedder/LLM accordingly, localize widget copy.
- Admin SPA (gated by Admin-Key) for tenant lifecycle + eval dashboards.
- mTLS / private-network bind for admin endpoints on public IPs.
