# Test Coverage Baseline

**Generated:** 2026-09-04

**Overall Coverage:** 81% (874 of 4518 statements missed)

This document documents the per-module test coverage baseline for the `app/` package, generated from running:
```
.venv/bin/python -m pytest --cov=app --cov-report=term-missing
```

## Per-Module Coverage Percentages

| Module | Statements | Covered | Missed | Percentage |
|--------|-----------|---------|--------|------------|
| app/__init__.py | 0 | 0 | 0 | 100% |
| app/analytics.py | 107 | 92 | 15 | 86% |
| app/audit.py | 74 | 74 | 0 | 100% |
| app/auth.py | 42 | 36 | 6 | 86% |
| app/backup.py | 77 | 32 | 45 | 42% |
| app/config.py | 58 | 58 | 0 | 100% |
| app/conversation.py | 137 | 122 | 15 | 89% |
| app/crypto.py | 79 | 72 | 7 | 91% |
| app/db.py | 349 | 277 | 72 | 79% |
| app/embed/__init__.py | 151 | 67 | 84 | 44% |
| app/eval.py | 171 | 122 | 49 | 71% |
| app/faithfulness.py | 114 | 80 | 34 | 70% |
| app/feedback.py | 41 | 33 | 8 | 80% |
| app/generation.py | 76 | 34 | 42 | 45% |
| app/ingestion/__init__.py | 95 | 94 | 1 | 99% |
| app/ingestion/chunker.py | 179 | 152 | 27 | 91% |
| app/ingestion/files.py | 83 | 59 | 24 | 71% |
| app/ingestion/html_util.py | 14 | 14 | 0 | 100% |
| app/ingestion/runner.py | 94 | 63 | 31 | 67% |
| app/ingestion/sitemap.py | 199 | 165 | 34 | 83% |
| app/ingestion/ssrf.py | 106 | 55 | 51 | 52% |
| app/jobs.py | 16 | 16 | 0 | 100% |
| app/knowledge_gaps.py | 51 | 41 | 10 | 80% |
| app/leads.py | 50 | 39 | 11 | 78% |
| app/main.py | 862 | 755 | 107 | 88% |
| app/models.py | 235 | 232 | 3 | 99% |
| app/observability.py | 148 | 140 | 8 | 95% |
| app/plans.py | 19 | 19 | 0 | 100% |
| app/quality_store.py | 20 | 20 | 0 | 100% |
| app/ratelimit.py | 125 | 83 | 42 | 66% |
| app/rbac.py | 25 | 23 | 2 | 92% |
| app/rerank/__init__.py | 56 | 43 | 13 | 77% |
| app/resilience.py | 123 | 88 | 35 | 72% |
| app/retrieval/__init__.py | 38 | 17 | 21 | 55% |
| app/retrieval/agentic.py | 44 | 40 | 4 | 91% |
| app/retrieval/rewrite.py | 58 | 50 | 8 | 86% |
| app/tenants.py | 83 | 63 | 20 | 76% |
| app/token_usage.py | 56 | 48 | 8 | 86% |
| app/usage.py | 74 | 70 | 4 | 95% |
| app/validation.py | 53 | 41 | 12 | 77% |
| app/vector_store.py | 136 | 101 | 35 | 74% |

## Key Observations

- **Lowest coverage modules** (priority for test additions):
  - `app/embed/__init__.py` — 44% (84 missed statements)
  - `app/backup.py` — 42% (45 missed statements)
  - `app/generation.py` — 45% (42 missed statements)
  - `app/ingestion/ssrf.py` — 52% (51 missed statements)
  - `app/retrieval/__init__.py` — 55% (21 missed statements)

- **Highest coverage modules** (well-tested):
  - `app/__init__.py` — 100%
  - `app/audit.py` — 100%
  - `app/config.py` — 100%
  - `app/jobs.py` — 100%
  - `app/quality_store.py` — 100%
  - `app/plans.py` — 100%

- **Overall suite**: 284 tests passed, 49 failed (pre-existing failures unrelated to coverage scope), 2 skipped.

## Next Steps

Per the backlog, the following items are planned based on this baseline:

1. Rank modules under `app/` by (low coverage AND high change-frequency in git log)
2. Add real unit tests for the riskiest under-tested modules
3. Introduce/repair `pytest` marks (`unit`, `integration`, `slow`, `external`)
4. Audit test fixtures for duplicated setup and consolidate into `conftest.py`