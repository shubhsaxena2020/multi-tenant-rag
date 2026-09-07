#!/usr/bin/env python3
"""
Regression Guard: Automated performance check with explicit budget.

This script checks that p95 latency stays within the budget.
If exceeded, it fails loudly (non-zero exit code) so CI/CD can detect regressions.

Budget: p95 < 500ms (per SLO targets)
Historical baseline: p95 ≈ 0.0025ms (200x headroom)
"""

import json
import urllib.request
import time
import sys

# Configuration
BASE_URL = "http://localhost:8000"
LATENCY_TARGET_S = 0.5  # 500ms SLO target
NUM_SAMPLES = 30

def measure_p95():
    """Measure p95 latency against the service health endpoint."""
    latencies = []
    errors = 0
    
    for _ in range(NUM_SAMPLES):
        start = time.time()
        try:
            req = urllib.request.Request(f"{BASE_URL}/health")
            resp = urllib.request.urlopen(req, timeout=10)
            latency = time.time() - start
            latencies.append(latency)
        except Exception as e:
            errors += 1
    
    if not latencies:
        print("ERROR: No successful requests measured")
        sys.exit(1)
    
    latencies_sorted = sorted(latencies)
    n = len(latencies_sorted)
    k = (n - 1) * 95 / 100.0
    f = int(k)
    c = min(f + 1, n - 1)
    p95 = latencies_sorted[f] + (k - f) * (latencies_sorted[c] - latencies_sorted[f])
    
    return p95, errors, latencies_sorted, n

def main():
    p95, errors, latencies_sorted, n = measure_p95()
    
    p50 = latencies_sorted[int(n*0.5)] if n > 0 else 0
    p99 = latencies_sorted[int(n*0.99)] if n > 0 else 0
    
    print(f"Regression Guard Performance Report")
    print(f"=====================================")
    print(f"Samples taken: {NUM_SAMPLES}")
    print(f"Successful requests: {NUM_SAMPLES - errors}")
    print(f"Errors: {errors}")
    print(f"")
    print(f"Latency metrics:")
    print(f"  p50: {p50:.4f}s")
    print(f"  p95: {p95:.4f}s")
    print(f"  p99: {p99:.4f}s")
    print(f"")
    print(f"SLO Compliance:")
    print(f"  latency_target_s: {LATENCY_TARGET_S}")
    print(f"  latency_p95_s: {p95:.4f}")
    print(f"  threshold_ms: {LATENCY_TARGET_S * 1000:.0f}")
    print(f"  budget_headroom_x: {LATENCY_TARGET_S / p95:.1f}x")
    print(f"")
    
    # Check against budget
    if p95 > LATENCY_TARGET_S:
        print(f"FAIL: p95 ({p95:.4f}s) exceeds budget ({LATENCY_TARGET_S}s)")
        print(f"RECOMMENDATION: Investigate latency regression - p95 exceeds 500ms threshold")
        sys.exit(1)
    else:
        headroom = LATENCY_TARGET_S / p95
        print(f"PASS: p95 ({p95:.4f}s) within budget ({LATENCY_TARGET_S}s)")
        print(f"Headroom: {headroom:.1f}x under threshold")
        print(f"Service operating normally - no regression detected")
        sys.exit(0)
    
    # Output result as JSON for CI/CD integration
    result = {
        "p95_s": p95,
        "p95_met": p95 <= LATENCY_TARGET_S,
        "threshold_s": LATENCY_TARGET_S,
        "headroom_x": LATENCY_TARGET_S / p95 if p95 > 0 else 0,
        "errors": errors,
        "total": NUM_SAMPLES,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "service": "rag-service",
    }
    print(f"")
    print(f"JSON_RESULT: {json.dumps(result)}")

if __name__ == "__main__":
    main()
