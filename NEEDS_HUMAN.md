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
- (none currently blocking the agent's in-flight work)

## Resolved
- B1: `gh` CLI unavailable → now working (PRs + issues created this session).
- Issue #4: re-ingestion duplicate chunks → fixed in PR #6, tagged v10.14-dedup-reingest.
- Issue #2: additive-column migration guard → merged (PR #3).
