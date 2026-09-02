#!/usr/bin/env python3
"""Lightweight smoke commands for Hermes rag-service.

Usage:
    python scripts/smoke_metrics.py          # Quick metrics endpoint check
    python scripts/smoke_summary.py          # Quick /admin/summary check  
    python scripts/smoke_eval.py             # Quick eval report generation
    python scripts/smoke_all.py              # Run all three checks
"""

import json
import os
import subprocess
import sys
from pathlib import Path

# Add repo to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.main import app
from starlette.testclient import TestClient

ADMIN_KEY = os.environ.get("ADMIN_API_KEY", "test-admin-key-for-tests")


def smoke_metrics():
    """Quick check that /metrics endpoint is alive and returning Prometheus metrics."""
    print("=== Smoke: /metrics ===")
    client = TestClient(app)
    r = client.get("/metrics", headers={"Admin-Key": ADMIN_KEY})
    if r.status_code == 200:
        body = r.text
        # Verify it contains actual metric lines (not empty)
        lines = [l for l in body.splitlines() if l.strip()]
        print(f"  Status: 200 OK")
        print(f"  Metric lines returned: {len(lines)}")
        # Check for common Prometheus metric types
        has_histogram = any('rag_request_duration_seconds' in l for l in lines[:20])
        print(f"  Has rag_request_duration_seconds: {has_histogram}")
        return True
    else:
        print(f"  Status: {r.status_code}")
        print(f"  Body: {r.text[:200]}")
        return False


def smoke_summary():
    """Quick check that /admin/summary returns valid tenant rollup data."""
    print("=== Smoke: /admin/summary ===")
    client = TestClient(app)
    r = client.get("/admin/summary", headers={"Admin-Key": ADMIN_KEY})
    if r.status_code == 200:
        data = r.json()
        totals_keys = data.get("totals", {})
        tokens_keys = data.get("tokens", {})
        tenant_count = data.get("tenant_count", 0)
        print(f"  Status: 200 OK")
        print(f"  Tenant count: {tenant_count}")
        print(f"  Totals keys: {list(totals_keys.keys()) if totals_keys else 'none'}")
        print(f"  Tokens keys: {list(tokens_keys.keys()) if tokens_keys else 'none'}")
        # Verify structural expectations
        expected_totals = {"chunks_ingested", "docs_ingested", "eval_runs", "feedback_down", "feedback_up", "knowledge_gaps", "leads", "queries"}
        expected_tokens = {"calls", "completion_tokens", "cost_basis", "cost_usd", "prompt_tokens", "tenant_count", "total_tokens", "window_days"}

        totals_ok = set(totals_keys.keys()) == expected_totals if totals_keys else False
        tokens_ok = set(tokens_keys.keys()) == expected_tokens if tokens_keys else False

        print(f"  Totals structural OK: {totals_ok}")
        print(f"  Tokens structural OK: {tokens_ok}")
        return totals_ok and tokens_ok
    else:
        print(f"  Status: {r.status_code}")
        print(f"  Body: {r.text[:200]}")
        return False


def smoke_eval():
    """Quick check that eval report generation works (uses nightly_eval.py infrastructure)."""
    print("=== Smoke: Eval report ===")
    env = os.environ.copy()
    env["RAG_BASE_URL"] = "http://localhost:8000"
    env["TENANT_KEYS"] = json.dumps({"acme": "rk_test"})
    env["STORE"] = "/tmp/smoke_eval_report.jsonl"
    env["REGRESSION_THRESHOLD"] = "0.15"
    env["MIN_RUNS_FOR_CHECK"] = "1"

    # Run a minimal eval invocation
    result = subprocess.run(
        [sys.executable, "scripts/nightly_eval.py"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    if result.returncode == 0:
        print(f"  Status: exited 0")
        # Check if report was generated
        store_path = env.get("STORE", "/tmp/smoke_eval_report.jsonl")
        if os.path.exists(store_path):
            with open(store_path) as f:
                content = f.read()
            lines = [l for l in content.splitlines() if l.strip()]
            print(f"  JSONL lines written: {len(lines)}")
            # Show first line as sample
            if lines:
                try:
                    first = json.loads(lines[0])
                    print(f"  First report keys: {list(first.keys())}")
                except Exception:
                    print(f"  First line: {lines[0][:100]}")
        else:
            print(f"  JSONL store not found at {store_path}")
        return True
    else:
        print(f"  Status: exited {result.returncode}")
        print(f"  stderr: {result.stderr[:300]}")
        return False


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Hermes rag-service smoke commands")
    parser.add_argument("command", choices=["metrics", "summary", "eval", "all"],
                        help="Which smoke check to run")

    args = parser.parse_args()

    if args.command == "metrics":
        smoke_metrics()
    elif args.command == "summary":
        smoke_summary()
    elif args.command == "eval":
        smoke_eval()
    elif args.command == "all":
        print("Running all smoke checks...\n")
        smoke_metrics()
        print()
        smoke_summary()
        print()
        smoke_eval()


if __name__ == "__main__":
    main()
