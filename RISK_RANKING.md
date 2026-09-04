# Module Risk Ranking by Coverage + Change Frequency

**Based on:** TEST_COVERAGE_BASELINE.md and git log change frequency

The 5 riskiest under-tested modules (combining low coverage with high change frequency):

| Rank | Module | Coverage | Git Commits | Risk Score* |
|------|--------|----------|-------------|-------------|
| 1 | `app/embed/__init__.py` | 44% | 5 | 5 × 84 = 420 |
| 2 | `app/backup.py` | 42% | 3 | 3 × 45 = 135 |
| 3 | `app/generation.py` | 45% | 6 | 6 × 42 = 252 |
| 4 | `app/ingestion/ssrf.py` | 52% | 5 | 5 × 51 = 255 |
| 5 | `app/retrieval/__init__.py` | 55% | 6 | 6 × 21 = 126 |

*\*Risk Score = (git commit count) × (missed statements), higher = riskier*

## Rationale

- **`app/embed/__init__.py`** (44% coverage, 84 missed): Lowest coverage + moderate change frequency. Embedding logic is core to RAG quality but has sparse test coverage.
- **`app/ingestion/ssrf.py`** (52% coverage, 51 missed): SSRF protection is security-critical; moderate coverage leaves attack paths untested.
- **`app/generation.py`** (45% coverage, 42 missed): Generation logic is high-value; low coverage means many code paths untested.
- **`app/backup.py`** (42% coverage, 45 missed): Backup/restore functionality with notably low coverage — data loss risk if regression occurs.
- **`app/retrieval/__init__.py`** (55% coverage, 21 missed): Despite mid-coverage, high change frequency (6 commits) makes it risky for regressions.

## Next Steps

Per backlog item [5] "Rank modules under `app/` by (low coverage AND high change-frequency in git log). Done when: the 5 riskiest under-tested modules are listed with numbers in the baseline doc."

The above ranking is now documented. Next: run the suite with `--randomly-seed` varied 5 times to identify order-dependent tests.