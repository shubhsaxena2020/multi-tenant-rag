#!/usr/bin/env python3
"""
Evidence capture script for RAG service milestone validation.

Purpose: Provide operator-verifiable metrics, dashboard states, and summary
endpoints so milestone validation can be reproduced without reconstructing
context from chat. This script captures all the concrete evidence needed
for release readiness verification.

Usage:
    python scripts/evidence_capture.py > evidence.txt

Or capture specific sections:
    python scripts/evidence_capture.py --section metrics
    python scripts/evidence_capture.py --section summary
    python scripts/evidence_capture.py --section tests
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import hashlib
from datetime import datetime
from pathlib import Path

# Ensure repo is in path
REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))


def run_tests() -> dict:
    """Run the core test suite and return results."""
    cmd = [
        sys.executable, "-m", "pytest",
        "tests/test_retrieval_observability.py",
        "tests/test_phase_e_analytics.py",
        "tests/test_phase_e_token_usage.py",
        "tests/test_slo_jobs.py",
        "tests/test_api.py::test_metrics_endpoint",
        "-q", "--tb=short"
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(REPO_ROOT))
    return {
        "command": " ".join(cmd),
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "passed": "passed" in result.stdout and "failed" not in result.stdout,
    }


def get_admin_summary() -> dict:
    """Capture /admin/summary output with Admin-Key."""
    import os, asyncio
    from contextlib import redirect_stdout
    import io

    env_vars = {
        "QDRANT_URL": ":memory:",
        "REDIS_URL": "",
        "ADMIN_API_KEY": "test-admin-key-for-tests",
        "USE_REAL_EMBEDDER": "0",
        "USE_REAL_RERANKER": "0",
        "MASTER_ENCRYPTION_KEY": "A" * 64,
    }

    old_env = {}
    for k, v in env_vars.items():
        old_env[k] = os.environ.get(k)
        os.environ[k] = v

    try:
        from app.config import get_settings
        from app.ratelimit import reset_limiter
        from app.vector_store import reset_client
        import app.db

        app.db._engine = None
        app.db._session_maker = None

        from app.db import init_db
        asyncio.run(init_db())

        from fastapi.testclient import TestClient
        from app.main import app

        c = TestClient(app)
        for _ in range(10):
            try:
                if c.get("/health").status_code == 200:
                    break
            except Exception:
                pass

        resp = c.get("/admin/summary", headers={"Authorization": "Bearer test-admin-key-for-tests"})
        return resp.json()
    finally:
        # Restore old environment
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def get_metrics() -> dict:
    """Capture /metrics endpoint output."""
    import os, asyncio
    from contextlib import redirect_stdout
    import io

    env_vars = {
        "QDRANT_URL": ":memory:",
        "REDIS_URL": "",
        "ADMIN_API_KEY": "test-admin-key-for-tests",
        "USE_REAL_EMBEDDER": "0",
        "USE_REAL_RERANKER": "0",
        "MASTER_ENCRYPTION_KEY": "A" * 64,
    }

    old_env = {}
    for k, v in env_vars.items():
        old_env[k] = os.environ.get(k)
        os.environ[k] = v

    try:
        from app.config import get_settings
        from app.ratelimit import reset_limiter
        from app.vector_store import reset_client
        import app.db

        app.db._engine = None
        app.db._session_maker = None

        from app.db import init_db
        asyncio.run(init_db())

        from fastapi.testclient import TestClient
        from app.main import app

        c = TestClient(app)
        for _ in range(10):
            try:
                if c.get("/health").status_code == 200:
                    break
            except Exception:
                pass

        resp = c.get("/metrics", headers={"Authorization": "Bearer test-admin-key-for-tests"})
        # Extract rag_ metrics
        lines = resp.text.splitlines()
        rag_lines = [l for l in lines if 'rag_' in l.lower() or 'query_health' in l or 'faithfulness' in l or 'no_answer' in l]
        return {
            "all_metric_count": len([l for l in lines if l.strip()]),
            "rag_metric_count": len(rag_lines),
            "rag_metrics": rag_lines[:30],  # first 30 for brevity
            "total_lines": len(lines),
        }
    finally:
        # Restore old environment
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def get_grafana_dashboard() -> dict:
    """Capture Grafana dashboard state."""
    dashboard_path = REPO_ROOT / "deploy" / "grafana" / "dashboards" / "rag-quality.json"
    if dashboard_path.exists():
        with open(dashboard_path) as f:
            content = f.read()
        try:
            data = json.loads(content)
            # Grafana dashboards have structure: {"dashboard": {...}, "overwrite": ...}
            dashboard_data = data.get("dashboard", data)
            panels = dashboard_data.get("panels", [])
            return {
                "file": str(dashboard_path),
                "exists": True,
                "panel_count": len(panels),
                "panel_titles": [p.get("title", "unknown") for p in panels],
                "metric_alignments": [
                    {
                        "panel": i,
                        "title": p.get("title", "unknown"),
                        "expr": p.get("targets", [{}])[0].get("expr", "") if p.get("targets") else "",
                    }
                    for i, p in enumerate(panels)
                ],
            }
        except Exception as e:
            return {"file": str(dashboard_path), "exists": True, "error": str(e)}
    return {"file": str(dashboard_path), "exists": False}


def get_changelog_snapshot() -> dict:
    """Capture the current CHANGELOG version block."""
    changelog_path = REPO_ROOT / "CHANGELOG.md"
    if changelog_path.exists():
        with open(changelog_path) as f:
            content = f.read()
        # Extract the current version block
        lines = content.splitlines()
        # Find the current version (first ## Version line)
        current_version = None
        for i, line in enumerate(lines):
            if line.startswith("## Version "):
                current_version = line
                break
        return {
            "file": str(changelog_path),
            "exists": True,
            "current_version": current_version,
            "total_lines": len(lines),
        }
    return {"file": str(changelog_path), "exists": False}


def get_release_evidence() -> dict:
    """Capture RELEASE_EVIDENCE.md current state."""
    evidence_path = REPO_ROOT / "RELEASE_EVIDENCE.md"
    if evidence_path.exists():
        with open(evidence_path) as f:
            content = f.read()
        return {
            "file": str(evidence_path),
            "exists": True,
            "total_lines": len(content.splitlines()),
        }
    return {"file": str(evidence_path), "exists": False}


def get_key_file_checksums() -> dict:
    """Compute SHA256 checksums for key reproducibility files."""
    key_files = [
        "app/observability.py",
        "deploy/alert.rules.yml",
        "deploy/grafana/dashboards/rag-quality.json",
        "scripts/nightly_eval.py",
        "RELEASE_EVIDENCE.md",
        "RELEASE-CHECKLIST.md",
    ]
    checksums = {}
    for f in key_files:
        fpath = REPO_ROOT / f
        if fpath.exists():
            with open(fpath, "rb") as fh:
                h = hashlib.sha256(fh.read()).hexdigest()
            checksums[f] = h[:16]
    return checksums


def main() -> int:
    """Main entry point - capture all evidence and print structured report."""
    print("=" * 60)
    print("RAG Service Milestone Evidence Capture")
    print(f"Generated: {datetime.utcnow().isoformat()} UTC")
    print("=" * 60)
    print()

    # 1. Test suite results
    print("1. TEST SUITE RESULTS")
    print("-" * 40)
    test_results = run_tests()
    print(f"   Command: {test_results['command']}")
    print(f"   Return code: {test_results['returncode']}")
    print(f"   Passed: {test_results['passed']}")
    if test_results["stdout"]:
        # Show summary line
        for line in test_results["stdout"].splitlines():
            if "passed" in line and "failed" not in line:
                print(f"   {line.strip()}")
                break
    print()

    # 2. Admin summary
    print("2. /admin/summary ENDPOINT")
    print("-" * 40)
    summary = get_admin_summary()
    if isinstance(summary, dict) and "tenant_count" in summary:
        print(f"   Tenant count: {summary.get('tenant_count')}")
        print(f"   Totals keys: {list(summary.get('totals', {}).keys())}")
        print(f"   Tokens keys: {list(summary.get('tokens', {}).keys())}")
    else:
        print(f"   Raw output: {json.dumps(summary, indent=2)[:500]}")
    print()

    # 3. Metrics endpoint
    print("3. /metrics ENDPOINT")
    print("-" * 40)
    metrics = get_metrics()
    if isinstance(metrics, dict) and "rag_metric_count" in metrics:
        print(f"   Total metric lines: {metrics.get('all_metric_count')}")
        print(f"   RAG-related metric lines: {metrics.get('rag_metric_count')}")
    else:
        print(f"   Output: {str(metrics)[:500]}")
    print()

    # 4. Grafana dashboard
    print("4. GRANA DASHBOARD")
    print("-" * 40)
    dashboard = get_grafana_dashboard()
    if dashboard.get("exists"):
        print(f"   File: {dashboard['file']}")
        print(f"   Panels: {dashboard.get('panel_count', 0)}")
        for title in dashboard.get("panel_titles", []):
            print(f"     - {title}")
    else:
        print(f"   Dashboard not found")
    print()

    # 5. CHANGELOG
    print("5. CHANGELOG")
    print("-" * 40)
    changelog = get_changelog_snapshot()
    if changelog.get("exists"):
        print(f"   Current version: {changelog.get('current_version')}")
        print(f"   Total lines: {changelog.get('total_lines')}")
    print()

    # 6. RELEASE_EVIDENCE.md
    print("6. RELEASE_EVIDENCE.md")
    print("-" * 40)
    evidence = get_release_evidence()
    if evidence.get("exists"):
        print(f"   File: {evidence['file']} ({evidence['total_lines']} lines)")
    print()

    # 7. Key file checksums for reproducibility
    print("7. KEY FILES (checksums for reproducibility)")
    print("-" * 40)
    checksums = get_key_file_checksums()
    for f, h in checksums.items():
        print(f"   {f}: {h}...")
    print()

    # 8. Smoke commands summary
    print("8. SMOKE COMMANDS (quick operator verification)")
    print("-" * 40)
    try:
        result = subprocess.run(
            [".venv/bin/python", "scripts/smoke_commands.py", "metrics"],
            capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=30
        )
        metrics_ok = result.returncode == 0
        print(f"   metrics: {'PASS' if metrics_ok else 'FAIL'} — check /metrics endpoint")
    except Exception as e:
        print(f"   metrics: ERROR — {e}")
    print()

    print("=" * 60)
    print("Evidence capture complete.")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())