"""PHASE E (#25-#29) — retrieval-quality observability.

- A query persists a row to query_quality_events (latency, hit_count, faithfulness, ...).
- /metrics exposes the new quality series (rag_faithfulness, rag_no_answer_total).
- The Grafana dashboard JSON is valid JSON and references the new series.
- The nightly eval script is importable and validates its env config.
"""

import json

import pytest

from app.quality_store import record_quality_bg
from sqlalchemy import text


def _make_tenant(client, name, plan="standard"):
    r = client.post(
        "/api/v1/tenants", headers={"Admin-Key": "test-admin-key-for-tests"},
        json={"name": name, "plan": plan},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


def _count_quality_rows(tenant_id: str) -> int:
    from app.db import get_session_maker
    import asyncio

    async def _c():
        sm = get_session_maker()
        async with sm() as s:
            await s.execute(text("""
                CREATE TABLE IF NOT EXISTS query_quality_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL,
                    latency_ms REAL,
                    hit_count INTEGER,
                    faithfulness REAL,
                    rerank_delta REAL,
                    rewrite_used INTEGER,
                    multi_hop INTEGER,
                    no_answer INTEGER,
                    meta TEXT,
                    ts TIMESTAMP WITH TIME ZONE NOT NULL
                )
            """))
            res = await s.execute(text("SELECT COUNT(*) FROM query_quality_events WHERE tenant_id = :t"), {"t": tenant_id})
            return res.scalar() or 0
    return asyncio.run(_c())


def test_query_persists_quality_event(client):
    t = _make_tenant(client, "qualityobs")
    doc = client.post(
        f"/api/v1/{t['tenant_id']}/documents", headers=_auth(t["api_key"]),
        json={"title": "doc", "content": "The Phoenix project launched in Q1 with the billing service.", "content_type": "text"},
    )
    assert doc.status_code == 201, doc.text
    before = _count_quality_rows(t["tenant_id"])
    q = client.post(
        f"/api/v1/{t['tenant_id']}/query", headers=_auth(t["api_key"]),
        json={"question": "When did Phoenix launch?", "top_k": 3, "generate": True},
    )
    assert q.status_code == 200, q.text
    # record_quality_bg runs on a daemon thread; give it a brief moment.
    import time
    for _ in range(50):
        if _count_quality_rows(t["tenant_id"]) > before:
            break
        time.sleep(0.05)
    assert _count_quality_rows(t["tenant_id"]) > before, "query did not persist a quality event"


def test_metrics_expose_quality_series(client):
    r = client.get("/metrics", headers={"Admin-Key": "test-admin-key-for-tests"})
    assert r.status_code == 200, r.text
    body = r.text
    assert "rag_faithfulness" in body
    assert "rag_no_answer_total" in body
    assert "rag_query_rewrite_total" in body


def test_grafana_dashboard_valid_and_references_series():
    path = "deploy/grafana/dashboards/rag-quality.json"
    with open(path) as fh:
        dash = json.load(fh)
    assert dash["dashboard"]["uid"] == "rag-quality"
    exprs = " ".join(
        t.get("expr", "") for p in dash["dashboard"]["panels"] for t in p.get("targets", [])
    )
    assert "rag_faithfulness" in exprs
    assert "rag_no_answer_total" in exprs


def test_nightly_eval_script_validates_env(monkeypatch):
    # Importable + rejects empty/invalid TENANT_KEYS without crashing.
    import importlib.util
    spec = importlib.util.spec_from_file_location("nightly_eval", "scripts/nightly_eval.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setenv("TENANT_KEYS", "not-json")
    assert mod.main() == 2
    monkeypatch.setenv("TENANT_KEYS", "{}")
    assert mod.main() == 2
