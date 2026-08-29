#!/usr/bin/env python3
"""PHASE E (#28) — nightly retrieval-quality eval hook.

Cron-able script that runs the per-tenant golden-set evaluation and appends the report to a local
JSONL store so quality can be trended over time (complements the Prometheus/SQLite signals). It
reuses the existing POST /api/v1/{tenant}/eval/run and /eval/quality endpoints, so no eval logic
is duplicated.

Usage:
  RAG_BASE_URL=http://localhost:8000 \
  TENANT_KEYS='{"acme":"rk_xxx","globex":"rk_yyy"}' \
  STORE=/var/lib/rag/nightly_eval.jsonl \
  python scripts/nightly_eval.py

TENANT_KEYS is a JSON object mapping tenant_id -> secret API key (X-API-Key). The script is
best-effort: if a tenant has no golden set it is skipped with a warning rather than failing the run.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

try:
    import requests
except ImportError:  # pragma: no cover - requests is a common dep; message is actionable
    sys.stderr.write("requests is required: pip install requests\n")
    sys.exit(2)


def _post(base: str, tenant: str, key: str, path: str) -> dict | None:
    url = f"{base}/api/v1/{tenant}{path}"
    r = requests.post(url, headers={"X-API-Key": key}, timeout=120)
    if r.status_code == 400 and "no golden set" in r.text:
        sys.stderr.write(f"[warn] tenant={tenant} has no golden set; skipping\n")
        return None
    r.raise_for_status()
    return r.json()


def main() -> int:
    base = os.environ.get("RAG_BASE_URL", "http://localhost:8000").rstrip("/")
    store = os.environ.get("NIGHTLY_EVAL_STORE", "nightly_eval.jsonl")
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
    failures = 0
    with open(store, "a", encoding="utf-8") as fh:
        for tenant, key in tenants.items():
            for path in ("/eval/run", "/eval/quality"):
                try:
                    report = _post(base, tenant, key, path)
                except Exception as exc:  # pragma: no cover - network best-effort
                    sys.stderr.write(f"[error] tenant={tenant} {path} failed: {exc}\n")
                    failures += 1
                    report = None
                if report is None:
                    continue
                record = {"ts": ts, "tenant": tenant, "endpoint": path, "report": report}
                fh.write(json.dumps(record) + "\n")
                fh.flush()
                sys.stdout.write(f"[ok] {tenant} {path} -> {json.dumps(report)[:200]}\n")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
