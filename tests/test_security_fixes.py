"""Regression tests for the v7 security + functional remediation (audit findings 1-9).

These exercise the FIXED behavior:
 #1 SSRF-safe URL fetch (reject private/loopback/link-local/metadata; no blind fetch)
 #2 RBAC acl server-enforced (tenant can't self-escalate to unprovisioned groups)
 #3 X-Forwarded-For only trusted behind a configured trusted proxy
 #4 ingest rate-limit window fixed (per-minute, not per-hour)
 #5 Prometheus label uses route template, not raw path
 #6 rerank score exposed and `score` matches returned order
 #7 (architecture) documented; not a behavioral unit test here
 #8 candidate_k default lowered (latency); assert new default
 #9 conversation: query rewrite resolves follow-ups; OOS + injection flag...[truncated]