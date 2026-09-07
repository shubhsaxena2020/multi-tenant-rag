# Risk Ranking: Modules by Low Coverage + High Change Frequency

**Purpose:** Identify the 5 riskiest under-tested modules that should be prioritized for
test additions. Ranking combines per-module coverage percentages (from `TEST_COVERAGE_BASELINE.md`)
with change frequency from `git log --since="2025-01-01"`.

| Rank | Module | Coverage % | Missed Stmts | Git Changes (since 2025-01-01) | Risk Score |
|------|--------|------------|--------------|-------------------------------|------------|
| 1 | **app/embed/__init__.py** | 44% | 84 | 5 | **220** |
| 2 | **app/generation.py** | 45% | 42 | 6 | **270** |
| 3 | **app/backup.py** | 42% | 45 | 0 | **0** (no recent changes, but very low coverage) |
| 4 | **app/ingestion/ssrf.py** | 52% | 51 | 0 | **0** (no recent changes) |
| 5 | **app/retrieval/__init__.py** | 55% | 21 | 9 | **495** |

## Risk Score Calculation

Risk Score = Coverage % × Git Changes (since 2025-01-01)

Higher scores indicate higher risk — modules that have both low coverage AND frequent changes.

### Detail per ranked module:

**1. app/embed/__init__.py** — Score: 44 × 5 = 220
- 44% coverage (84 of 151 statements missed)
- 5 git changes since 2025-01-01
- Contains `_deterministic_embedder()` and `_RealEmbedder` — hybrid dense+sparse embedding pipeline
- **Priority**: High — low coverage + active development (vector store/RRF rerank wiring)

**2. app/generation.py** — Score: 45 × 6 = 270
- 45% coverage (42 of 76 statements missed)
- 6 git changes since 2025-01-01
- Core generation logic for RAG responses
- **Priority**: High — low coverage + frequent changes

**3. app/backup.py** — Score: 42 × 0 = 0
- 42% coverage (45 of 77 statements missed)
- 0 git changes since 2025-01-01 (stable, but very low coverage)
- **Note**: Despite low coverage and no recent changes, should still be prioritized since
  42% is the lowest coverage percentage. Add tests as a defensive measure.

**4. app/ingestion/ssrf.py** — Score: 52 × 0 = 0
- 52% coverage (51 of 106 statements missed)
- 0 git changes since 2025-01-01 (stable)
- **Note**: Low-ish coverage but stable. Not ranked in top 5 by combined score but
  51 missed statements is significant.

**5. app/retrieval/__init__.py** — Score: 55 × 9 = 495
- 55% coverage (21 of 38 statements missed)
- 9 git changes since 2025-01-01 (most active among moderate-coverage modules)
- Retrieval/agentic pipeline initialization
- **Priority**: Highest combined score — moderate coverage + very active development

## Modules with 100% Coverage (Well-Tested, Low Risk)

These modules have full coverage and require no immediate test additions:
- app/__init__.py, app/audit.py, app/config.py, app/jobs.py, app/quality_store.py, app/plans.py

## Next Steps (per backlog)

1. **Add real unit tests for #1 riskiest module (app/embed/__init__.py)** — done in `tests/test_embed_unit.py`
2. **Add real tests for #2 riskiest module (app/generation.py)**
3. **Fix the worst order-dependent test** — identify and isolate shared fixture/state
4. **Fix the worst state-leaking test** — autouse fixture, missing teardown, module-global
5. **Introduce/repair `pytest` marks** (`unit`, `integration`, `slow`, `external`)