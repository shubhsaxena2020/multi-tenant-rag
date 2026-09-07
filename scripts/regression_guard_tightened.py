#!/usr/bin/env python3
"""Regression guard: tighten automated performance check with explicit budget.

Measures p95 latency against http://localhost:8000/health and compares
against 400ms budget. Fails loudly with non-zero exit code on regression,
outputs JSON for CI/CD integration.

Usage: python3 regression_guard_tightened.py

SLO: p95 < 400ms (tightened from 500ms original budget)
"""

import json
import subprocess
import sys
import time
import urllib.request

# Budget: p95 < 400ms
BUDGET_P95_MS = 400
BUDGET_S = BUDGET_P95_MS / 1000.0

# Target endpoint
TARGET_URL = 'http://localhost:8000/health'

# Number of samples
SAMPLES = 30

def measure_latency():
    """Measure p50/p95/p99 latency against the health endpoint."""
    latencies = []
    errors = 0

    for i in range(SAMPLES):
        start = time.time()
        try:
            req = urllib.request.Request(TARGET_URL)
            resp = urllib.request.urlopen(req, timeout=10)
            latency = time.time() - start
            latencies.append(latency)
        except Exception as e:
            errors += 1
            print(f'Request {i} failed: {e}')

    if not latencies:
        print('ERROR: No successful requests')
        sys.exit(1)

    latencies_sorted = sorted(latencies)
    n = len(latencies_sorted)

    p50 = latencies_sorted[int(n * 0.5)] if n > 0 else 0
    k = (n - 1) * 95 / 100.0
    f = int(k)
    c = min(f + 1, n - 1)
    p95 = latencies_sorted[f] + (k - f) * (latencies_sorted[c] - latencies_sorted[f])
    p99 = latencies_sorted[int(n * 0.99)] if n > 0 else 0

    return {
        'p50_s': p50,
        'p95_s': p95,
        'p99_s': p99,
        'error_rate': errors / SAMPLES,
        'total_samples': SAMPLES,
        'budget_s': BUDGET_S,
        'budget_p95_ms': BUDGET_P95_MS,
    }

def main():
    result = measure_latency()

    # Check against budget
    slo_met = result['p95_s'] < result['budget_s']

    print('Regression Guard Performance Report')
    print('=====================================')
    print(f'Samples taken: {result["total_samples"]}')
    print(f'Successful requests: {SAMPLES - int(result["error_rate"] * SAMPLES)}')
    print(f'Errors: {int(result["error_rate"] * SAMPLES)}')

    print()
    print('Latency metrics:')
    print(f'  p50: {result["p50_s"]:.4f}s')
    print(f'  p95: {result["p95_s"]:.4f}s')
    print(f'  p99: {result["p99_s"]:.4f}s')

    print()
    print('SLO Compliance:')
    print(f'  latency_target_s: {result["budget_s"]}')
    print(f'  latency_p95_s: {result["p95_s"]:.4f}')
    print(f'  threshold_ms: {result["budget_p95_ms"]}')
    headroom = result['budget_s'] / result['p95_s'] if result['p95_s'] > 0 else float('inf')
    print(f'  budget_headroom_x: {headroom:.1f}x')

    print()
    if slo_met:
        print(f'PASS: p95 ({result["p95_s"]:.4f}s) within budget ({result["budget_s"]}s)')
        print(f'Headroom: {headroom:.1f}x under threshold')
    else:
        print(f'FAIL: p95 ({result["p95_s"]:.4f}s) EXCEEDS budget ({result["budget_s"]}s)')
        print(f'Overage: {result["p95_s"] - result["budget_s"]:.4f}s')

    print()
    print('Service operating normally - no regression detected' if slo_met else 'REGRESSION DETECTED - latency exceeds budget')

    # Output JSON for CI/CD integration
    evidence = {
        'p50_s': result['p50_s'],
        'p95_s': result['p95_s'],
        'p99_s': result['p99_s'],
        'error_rate': result['error_rate'],
        'total_samples': result['total_samples'],
        'budget_p95_ms': result['budget_p95_ms'],
        'budget_s': result['budget_s'],
        'slo_met': slo_met,
        'headroom_x': headroom,
        'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
    }

    evidence_path = '/tmp/regression_guard_evidence_tightened.json'
    with open(evidence_path, 'w') as f:
        json.dump(evidence, f, indent=2)

    print(f'Evidence artifact: {evidence_path}')

    # Fail loudly on regression
    if not slo_met:
        sys.exit(1)

    sys.exit(0)

if __name__ == '__main__':
    main()
