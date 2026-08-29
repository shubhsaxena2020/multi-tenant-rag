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
