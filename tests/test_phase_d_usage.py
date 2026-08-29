"""PHASE D — per-tenant usage metering & business reporting (issue #27).

Real verification against the in-process app (TestClient):
- Ingesting docs + chunks, running queries, and running eval all increment the reliable
  per-tenant usage counters (distinct from the sampled audit trail / global prometheus).
- GET /api/v1/{tenant}/usage (tenant secret key) returns the summary.
- GET /admin/usage/{tenant} (admin only) returns summary + daily timeseries; ?fmt=csv yields a
  real CSV attachment; a missing tenant -> 404; a non-admin key -> 403.
"""
import os

from app.main import app

V = "/api/v1"
ADMIN = {"Admin-Key": os.environ.get("ADMIN_API_KEY", "test-admin-key-for-tests")}


def _mk(client, name="biz"):
    r = client.post(f"{V}/tenants", json={"name": name, "plan": "standard"}, headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()


def test_usage_metering_counts_events(client):
    t = _mk(client)
    key = t["api_key"]
    tid = t["tenant_id"]
    auth = {"Authorization": f"Bearer {key}"}

    # ingest two docs (each -> 1 doc_ingested + N chunk_ingested)
    for i in range(2):
        r = client.post(f"{V}/{tid}/documents", headers=auth,
                        json={"title": f"doc{i}", "content": f"France capital Paris number {i}. " * 10,
                              "content_type": "text"})
        assert r.status_code == 201, r.text

    # run 3 queries (sync endpoint)
    for _ in range(3):
        r = client.post(f"{V}/{tid}/query", headers=auth,
                        json={"question": "capital of France?", "generate": False, "top_k": 2})
        assert r.status_code == 200, r.text

    # usage summary
    r = client.get(f"{V}/{tid}/usage", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["docs_ingested"] >= 2, body
    assert body["chunks_ingested"] >= 2, body
    assert body["queries"] == 3, body


def test_admin_usage_report_and_csv(client):
    t = _mk(client, "report")
    key = t["api_key"]
    tid = t["tenant_id"]
    auth = {"Authorization": f"Bearer {key}"}

    client.post(f"{V}/{tid}/documents", headers=auth,
                json={"title": "doc", "content": "The capital of France is Paris. " * 8, "content_type": "text"})
    for _ in range(2):
        client.post(f"{V}/{tid}/query", headers=auth, json={"question": "q?", "generate": False})

    # admin JSON report
    r = client.get(f"/admin/usage/{tid}", headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "summary" in body and "timeseries" in body
    assert body["summary"]["docs_ingested"] >= 1
    assert body["summary"]["queries"] == 2
    assert isinstance(body["timeseries"], list)

    # admin CSV export
    r = client.get(f"/admin/usage/{tid}?fmt=csv", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv"), r.headers
    text = r.text
    assert text.startswith("date,queries,docs_ingested,chunks_ingested,eval_runs,total")
    # the CSV must carry the 2 queries we generated
    data_lines = [l for l in text.strip().splitlines()[1:] if l]
    total_queries = sum(int(l.split(",")[1]) for l in data_lines)
    assert total_queries == 2, text


def test_admin_usage_requires_admin_key(client):
    t = _mk(client, "noauth")
    tid = t["tenant_id"]
    # no admin header -> 403 (fail-closed, like /metrics)
    r = client.get(f"/admin/usage/{tid}")
    assert r.status_code == 403, r.text


def test_admin_usage_missing_tenant_404(client):
    r = client.get("/admin/usage/does_not_exist", headers=ADMIN)
    assert r.status_code == 404, r.text


def test_usage_respects_time_window(client):
    """get_usage_summary only counts within `days`; older events are excluded."""
    from app.usage import record_usage, get_usage_summary
    from datetime import datetime, timedelta, timezone

    tid = "t_window_test"
    # record a query "now"
    import asyncio
    asyncio.run(record_usage(tid, "query"))

    # record a query 400 days ago by directly manipulating ts via a second insert path
    from app.db import get_session_maker
    from sqlalchemy import text as sa_text
    import json
    async def _old():
        sm = get_session_maker()
        async with sm() as s:
            old = datetime.now(timezone.utc) - timedelta(days=400)
            await s.execute(sa_text(
                "INSERT INTO usage_events (tenant_id, kind, count, meta, ts) "
                "VALUES (:tid, 'query', 1, :m, :ts)"),
                {"tid": tid, "m": json.dumps({}), "ts": old})
            await s.commit()
    asyncio.run(_old())

    summary = asyncio.run(get_usage_summary(tid, days=30))
    assert summary.queries == 1, summary  # only the recent one
    summary_all = asyncio.run(get_usage_summary(tid, days=3650))
    assert summary_all.queries == 2, summary_all  # includes the old one
