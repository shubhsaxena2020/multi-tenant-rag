# NEEDS_HUMAN — genuine external blockers (not fabricatable by the agent)

The agent does NOT fake progress on these; they are recorded here and surfaced in chat.
See also EXPLICIT HUMAN CHECKPOINTS in /home/ubuntu/rag_v10_goal.txt (real LLM keys, public
DNS/SSL, live webhook creds, live DB migrations, billing).

## Status of previously-filed blockers

### B1 (was: `gh` CLI not installed) — RESOLVED
`gh` is installed and authenticated as **shubhsaxena2020** with `repo` scope. PRs and issues
now work. Evidence this session:
- Issue #4 filed (re-ingestion duplicates).
- Branch `feat/dedup-content-hash-reingest` pushed, PR #6 opened, self-merged (squash) to
  `master`, tagged `v10.14-dedup-reingest`.

### B2 (dirty tree + master/origin divergence) — MITIGATED
- The dirty working tree on `feat/phase-c-clickable-citations` (issue #2 migration-guard +
  key-tier column WIP) was **stashed** (preserved, not lost) at stash@{0}:
  `issue#2 migration-guard + key-tier columns (WIP, preserved for separate PR)`.
- `master` had diverged from `origin/master` (19 local-only commits: v10.5–v10.13 PHASE B
  feature work). Those commits are **backed up** on local branch
  `backup/local-master-pre-sync-<date>` before `master` was reset to `origin/master`.
- Action for human (optional): restore the stashed issue #2 work to its own PR branch and/or
  decide whether the backed-up v10.5–v10.13 work should be rebased onto current `origin/master`
  and PR'd separately. None of it is lost.

## Open blockers / human decisions
- **PHASE C npm publish (issue #14) — BLOCKED, needs human credential.**
  The JS SDK skeleton is committed under `sdk-js/` (`package.json`, `tsconfig.json`,
  `src/index.ts`, `README.md`) and is intended to publish as `@hermes-rag/sdk`. The actual
  `npm publish` requires an **npm registry token / publish access**, which is an external
  credential not available in this environment — the agent cannot fabricate it. The Python
  SDK (`sdk.py`) is complete and tested. Human action: run `cd sdk-js && npm install &&
  npm run build && npm publish --access public` with a logged-in npm account. (Until then the
  package can be consumed directly from `sdk-js/src/index.ts`.)

## Resolved
- B1: `gh` CLI unavailable → now working (PRs + issues created this session).
- Issue #4: re-ingestion duplicate chunks → fixed in PR #6, tagged v10.14-dedup-reingest.
- Issue #2: additive-column migration guard → merged (PR #3).
- Issue #14 (PHASE C widget+SDK polish): widget served (/widget.js,/widget.html,/demo), widget.js
  auth bug fixed, clickable citations + safe markdown + multi-turn added, hosted demo page added,
  SDK expanded (upload_file/ingest_sitemap/list_documents) + JS SDK skeleton. Merged PR #17,
  tagged v10.18-phase-c-widget-sdk. Only the npm *publish* step remains (external blocker above).
- Issue #22 (PHASE A: safe Markdown rendering in the embeddable widget): DONE. Replaced the
  plain-text `renderMarkdownSafe` with a strict allow-list `renderMarkdown()` that emits only a
  fixed set of safe tags (`<strong>/<em>/<code>/<a>/<ul>/<ol>/<li>/<blockquote>/<pre>/<h1-3>`);
  every text run is HTML-escaped and links are restricted to http(s) via `safeHref`. XSS-safe by
  construction (verified by tests/test_widget_safe_markdown.py: malicious `<script>`, `<img
  onerror>`, and `javascript:` links are neutralised). Bot answers + citation titles rendered
  through it; user messages stay `textContent`. Merged PR #49-era Phase A, tagged
  v16.48-widget-safe-md.

## Open blockers / human decisions (operator-setup, not agent-fabricatable)
These are real next-chapter items flagged in RESEARCH-NEXT-CHAPTER.md §3 that need human-owned
credentials/decisions. Recorded, not blocked-on:
- **SSO / SAML / OIDC login + SCIM** for the admin console — needs an IdP account/decision.
- **SOC 2 / HIPAA formal controls & audit-export packaging** — needs operator/legal ownership.
- **External monetization (Stripe billing, public pricing page)** — needs a Stripe account +
  business decision; underlying metering (token usage, chunk quota, `plan`) already exists and is
  exposed read-only.
- **npm publish of `@hermes-rag/sdk`** — needs an npm token (see B1/open item above).
- **Public DNS + TLS for hosted widget/API** — needs domain + cert (infra decision).
- **Per-tenant request rate limiting (KL-2, Phase I)** — `rate_limit()` exists but is fleet-wide /
  best-effort, not a per-tenant quota enforced at the edge. The per-tenant *chunk* quota
  (`TENANT_CHUNK_QUOTA`) is enforced at ingest, but there is no per-tenant *request-rate* ceiling yet.
  Tracked as Phase I work; recorded here so the gap is visible. Not in this agent's scope to add
  (it is tenancy/key-tier-adjacent and overlaps the orchestrator's P0/P1 hardening).
