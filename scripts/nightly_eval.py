#!/usr/bin/env python3
"""PHASE E (#28) — nightly retrieval-quality eval hook with regression detection.

Cron-able script that runs the per-tenant golden-set evaluation and appends the report to a local
JSONL store so quality can be trended over time (complements the Prometheus/SQLite signals). It
reuses the existing POST /api/v1/{tenant}/eval/run and /eval/quality endpoints, so no eval logic
is duplicated.

It ALSO detects quality regressions by comparing the latest run against the trailing N runs
and emits severity-level alerts to stderr when thresholds are crossed.

Enhanced: per-tenant trend tracking (improving/stable/regressing) and severity categorization
(minor/moderate/critical) based on quality-delta magnitude over the observation window.

Usage:
  RAG_BASE_URL=http://localhost:8000 \
  TENANT_KEYS='{"acme":"rk_xxx","globex":"rk_yyy"}' \
  STORE=/var/lib/rag/nightly_eval.jsonl \
  REGRESSION_THRESHOLD=0.15 \
  python scripts/nightly_eval.py

TENANT_KEYS is a JSON object mapping tenant_id -> secret API key (X-API-Key). The script is
best-effort: if a tenant has no golden set it is skipped with a warning rather than failing the run.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


# Thresholds for severity categorization (quality delta, negative = regression)
MINOR_DELTA = 0.05   # |delta| < 0.05 = minor variance
MODERATE_DELTA = 0.10  # 0.05 <= |delta| < 0.10 = moderate
CRITICAL_DELTA = 0.15  # |delta| >= 0.10 = critical (default REGRESSION_THRESHOLD)


def classify_severity(delta: float, abs_threshold: float = CRITICAL_DELTA) -> str:
    """Classify the severity of a quality delta.

    Args:
        delta: Quality value change (latest - previous). Negative = regression.
        abs_threshold: Absolute threshold for critical severity.

    Returns:
        Severity string: 'improving', 'stable', 'minor_regression',
        'moderate_regression', 'critical_regression'.
    """
    if delta is None:
        return "unknown"

    # Positive delta = improving quality
    if delta > 0:
        if delta >= abs_threshold:
            return "improving"  # Significant improvement
        return "stable"  # Small improvement, within normal variance

    # Negative delta = regression
    abs_delta = abs(delta)
    if abs_delta >= abs_threshold:
        return "critical_regression"  # Exceeds critical threshold
    if abs_delta >= MODERATE_DELTA:
        return "moderate_regression"  # Moderate regression
    return "minor_regression"  # Minor variance


def _post(base: str, tenant: str, key: str, path: str) -> dict | None:
    """POST to /api/v1/{tenant}{path} with X-API-Key auth.

    Returns parsed JSON response, or None if tenant has no golden set (400 + "no golden set").
    """
    import requests  # noqa: import-ok — best-effort network call; import at function scope

    url = f"{base}/api/v1/{tenant}{path}"
    r = requests.post(url, headers={"X-API-Key": key}, timeout=120)
    if r.status_code == 400 and "no golden set" in r.text.lower():
        sys.stderr.write(f"[warn] tenant={tenant} has no golden set; skipping\n")
        return None
    r.raise_for_status()
    return r.json()


def _extract_metrics(report: dict, required: Tuple[str, ...] = ("faithfulness", "hit_rate", "mrr", "context_recall")) -> Dict[str, Optional[float]]:
    """Extract known metrics from a report dict.

    Returns a dict mapping metric name to float value (or None if absent).
    """
    result: Dict[str, Optional[float]] = {}
    for key in required:
        result[key] = report.get(key)
    return result


def _compute_deltas(
    latest: dict,
    trailing: List[dict],
) -> Dict[str, Optional[float]]:
    """Compute latest-minus-previous delta for each metric across trailing runs.

    For each metric, the delta is latest - previous. If multiple trailing runs exist,
    the delta against the most recent previous run is returned.
    """
    latest_metrics = _extract_metrics(latest.get("report", {}))
    trailing_metrics = [_extract_metrics(r.get("report", {})) for r in trailing]

    deltas: Dict[str, Optional[float]] = {}
    for key in latest_metrics:
        latest_val = latest_metrics[key]
        if latest_val is None:
            deltas[key] = None
            continue

        # Use the most recent previous run's value
        prev_val: Optional[float] = None
        for run in reversed(trailing_metrics):
            prev = run.get(key)
            if prev is not None:
                prev_val = prev
                break

        if prev_val is None:
            deltas[key] = None
        else:
            deltas[key] = latest_val - prev_val

    return deltas


def _detect_regression(
    report: dict,
    history: List[dict],
    threshold: float = 0.15,
) -> List[Tuple[str, float, str]]:
    """Detect quality regressions by comparing latest report against trailing history.

    Returns a list of (metric, delta, severity) tuples for each detected change.
    Empty list if no significant changes detected.
    """
    trailing = history[-4:] if len(history) > 4 else history[:-1]  # most recent N before current

    if len(trailing) < 2:
        # Not enough history for comparison
        return []

    deltas = _compute_deltas(report, trailing)
    results: List[Tuple[str, float, str]] = []

    for key, delta in deltas.items():
        if delta is None:
            continue

        severity = classify_severity(delta, threshold)
        if severity == "unknown":
            continue

        results.append((key, round(delta, 4), severity))

    return results if results else []


def main() -> int:
    """Main entry point for the nightly eval hook.

    Returns exit code: 1 if regressions detected or runs completed, 0 otherwise.
    """
    base = os.environ.get("RAG_BASE_URL", "http://localhost:8000").rstrip("/")
    store = os.environ.get("NIGHTLY_EVAL_STORE", "nightly_eval.jsonl")
    regression_threshold = float(os.environ.get("REGRESSION_THRESHOLD", "0.15"))

    raw = os.environ.get("TENANT_KEYS", "{}")
    try:
        tenants = json.loads(raw)
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"TENANT_KEYS must be valid JSON: {exc}\n")
        return 2
    if not isinstance(tenants, dict) or not tenants:
        sys.stderr.write("TENANT_KEYS must be a non-empty JSON object {tenant_id: secret_key}\n")
        return 2

    ts = datetime.now(timezone.utc).isoformat()
    regression_detected = False

    with open(store, "a", encoding="utf-8") as fh:
        for tenant, key in tenants.items():
            for path in ("/eval/run", "/eval/quality"):
                try:
                    report = _post(base, tenant, key, path)
                except Exception as exc:  # network best-effort
                    sys.stderr.write(f"[error] tenant={tenant} {path} failed: {exc}\n")
                    regression_detected = True
                    report = None
                if report is None:
                    continue

                # Store the record for trend tracking
                record = {"ts": ts, "tenant": tenant, "endpoint": path, "report": report}
                fh.write(json.dumps(record) + "\n")
                fh.flush()

                # Emit per-tenant success line
                faith = report.get("faithfulness", 0)
                sys.stdout.write(f"[ok] {tenant} {path} -> faith={faith:.3f}\n")

                # Trend/regression detection using stored history
                try:
                    # Read all records for this tenant from the store
                    all_records: List[dict] = []
                    with open(store, "r", encoding="utf-8") as rf:
                        for line in rf:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                rec = json.loads(line)
                                if rec.get("tenant") == tenant:
                                    all_records.append(rec)
                            except json.JSONDecodeError:
                                continue

                    # trailing = everything before the just-written current record
                    trailing_records = all_records[:-1]

                    # Perform regression detection
                    results = _detect_regression(report, trailing_records, regression_threshold)

                    if results:
                        regression_detected = True
                        for metric, delta, severity in results:
                            sys.stderr.write(
                                f"[ALERT] tenant={tenant} {metric}: delta {delta:+.3f} "
                                f"(severity: {severity})\n"
                            )
                except FileNotFoundError:
                    # Store just created; no trailing history yet — no regression possible
                    pass
                except Exception as exc:  # defensive
                    sys.stderr.write(f"[warn] tenant={tenant} trend analysis failed: {exc}\n")

    return 1 if regression_detected else 0


if __name__ == "__main__":
    raise SystemExit(main())