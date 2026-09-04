# Test Runtime Timing Summary

Based on running `pytest -o "addopts=" --durations=0` on individual test files:

## Test File Runtimes (seconds)

| File | Tests | Runtime |
|------|-------|---------|
| tests/test_security_fixes.py | ~44 collected | 26.73s (slowest: 8.78s per test) |
| tests/test_api.py | 24 collected | 29.60s (3 failed, 21 passed) |
| tests/test_backup.py | ~4 collected | 3.26s |
| tests/test_dedup.py | 5 collected | 6.22s |
| tests/test_eval_quality.py | ~4 collected | 4.56s |
| tests/test_faithfulness.py | 9 collected | 4.71s |
| tests/test_faithfulness_edge.py | ~1 collected | 0.00s |
| tests/test_faithfulness_edge_cases.py | ~1 collected | 0.00s |
| tests/test_file_upload.py | ~10 collected | 6.40s |
| tests/test_ingestion_fidelity.py | ~5 collected | 2.70s |
| tests/test_injection_quarantine.py | ~3 collected | 4.36s |
| tests/test_key_scope_verification.py | 6 collected | 5.65s (2 failed) |
| tests/test_no_answer_faithfulness.py | ~1 collected | 2.44s |
| tests/test_faithfulness_edge.py | ~1 collected | 0.00s |
| tests/test_faithfulness_edge_cases.py | ~1 collected | 0.00s |

## Slowest Files (by runtime)

1. **tests/test_security_fixes.py** — ~27s for full suite (highest count, complex tenant isolation tests)
2. **tests/test_api.py** — ~30s (most tests, tenant management operations)
3. **tests/test_file_upload.py** — ~6s for ~10 tests
4. **tests/test_dedup.py** — ~6s for 5 tests
5. **tests/test_key_scope_verification.py** — ~6s for 6 tests

## Target Runtime Budget

Based on initial measurements, the current test suite runtime is approximately **30-40 seconds** for the collectable subset. The full 335-item suite originally took 186 seconds (as shown in the first coverage run), but many tests have collection errors (VERSION1_ROUTE NameError).

Next steps per backlog:
- [ ] Item 3: Run the suite with randomization varied 5 times to identify order-dependent tests
- [ ] Item 4: Document the 10 slowest files with seconds and a target total-runtime budget