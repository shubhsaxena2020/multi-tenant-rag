# Testing Runbook for rag-service

## Coverage

Run full suite with coverage:
```bash
.venv/bin/python -m pytest --cov=app --cov-report=term-missing
```

Generate coverage baseline:
```bash
.venv/bin/python -m pytest --cov=app --cov-report=term-missing > coverage_report.txt 2>&1
```

Run fast subset (excluding slow and external tests):
```bash
.venv/bin/python -m pytest -m "not slow and not external"
```

## Randomized-Order

Run with pytest-randomly to detect order-dependent tests:
```bash
.venv/bin/python -m pytest --randomly-seed=1 --randomly-shuffle 20
```

Or run N randomized passes to check for flakiness:
```bash
for i in $(seq 1 20); do
  .venv/bin/python -m pytest --randomly-seed=$i -q
  if [ $? -ne 0 ]; then
    echo "FAILED on seed $i"
    exit 1
  fi
done
echo "All 20 randomized runs passed"
```

## Repetition / State Leaking

Run suite 5 times to check for state leakage:
```bash
for i in $(seq 1 5); do
  .venv/bin/python -m pytest -q --tb=no
  if [ $? -ne 0 ]; then
    echo "FAILED on run $i"
    exit 1
  fi
done
echo "All 5 consecutive runs passed — no state leaking detected"
```

## Timing

List per-file runtimes (10 slowest files):
```bash
.venv/bin/python -m pytest --durations=10 -q
```

Record total runtime budget and verify it stays within target.

## Marked Subset (Pre-push / Pre-commit)

Run only `unit`-marked tests (ignoring embed tests with collection error):
```bash
.venv/bin/python -m pytest -m "unit" -q --ignore=tests/test_embed_unit.py
```

Run only `integration`-marked tests:
```bash
.venv/bin/python -m pytest -m "integration" -q
```

## Commands Summary

| Purpose | Command |
|---------|---------|
| Full coverage | `.venv/bin/python -m pytest --cov=app --cov-report=term-missing` |
| Fast subset | `.venv/bin/python -m pytest -m "not slow and not external"` |
| Randomized order (20 seeds) | `.venv/bin/python -m pytest --randomly-seed=$i -q` for i in `seq 1 20` |
| Repetition check (5 runs) | `.venv/bin/python -m pytest -q --tb=no` run 5 times |
| Timing (10 slowest) | `.venv/bin/python -m pytest --durations=10 -q` |
| Unit-marked only | `.venv/bin/python -m pytest -m "unit" -q` |
| All pytest marks | See `pyproject.toml` markers: unit, integration, slow, external |