"""PHASE E — unified per-tenant analytics aggregate (issue #37).

Real verification against the in-process app (TestClient):
- Drive usage (queries in/out of scope), feedback (up/down with questions), and leads, then assert
  the aggregate combines them: query volume, out-of-scope rate (from enriched usage meta), feedback
  positive rate, leads, handoff->lead funnel, and top questions ranked from feedback+leads text.
- Admin gate 403 without Admin-Key; missing tenant 404; CSV export well-formed.
"""
import os

from app.main import app

V = "/api/v1"
ADMIN = {"Admin-Key": os.environ.get("ADMIN_API_KEY", "test-admin-key-for-tests")}


def _mk(client, name="analytics"):
    r = client.post(f"{V}/tenants", json={"name": name, "plan": "standard"}, headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()


def _ingest(client, tid, auth, text):
    client.post(f"{V}/{tid}/documents", headers=auth,
                json={"title": "doc", "content": text, "content_type": "text"})


def test_analytics_aggregate_combines_sources(client):
    t = _mk(client)
    key = t["api_key"]
    auth = {"Authorization": f"Bearer {key}"}
    tid = t["tenant_id"]
    _ingest(client, tid, auth, "The capital of France is Paris. " * 10)

    # 2 in-scope queries about France
    for _ in range(2):
        r = client.post(f"{V}/{tid}/query", headers=auth,
                        json={"question": "capital of France?", "generate": False, "top_k": 2})
        assert r.status_code == 200, r.text
    # 1 out-of-scope query
    r = client.post(f"{V}/{tid}/query", headers=auth,
                    json={"question": "quantum computing trends 2050?", "generate": False, "top_k": 2})
    assert r.status_code == 200, r.text

    # feedback: one up (with question), one down (with question)
    client.post(f"{V}/{tid}/feedback", headers=auth,
                json={"rating": "up", "question": "capital of France?"})
    client.post(f"{V}/{tid}/feedback", headers=auth,
                json={"rating": "down", "question": "quantum computing trends 2050?"})

    # a lead for the out-of-scope question
    client.post(f"{V}/{tid}/handoff", headers=auth,
                json={"question": "quantum computing trends 2050?", "email": "lead@example.com"})

    r = client.get(f"/admin/analytics/{tid}", headers=ADMIN)
    assert r.status_code == 200, r.text
    a = r.json()
    assert a["usage"]["queries"] == 3, a
    assert a["out_of_scope"]["queries"] == 3
    assert a["out_of_scope"]["out_of_scope"] == 1
    assert abs(a["out_of_scope"]["out_of_scope_rate"] - 1 / 3) < 1e-9
    assert a["feedback"]["up"] == 1 and a["feedback"]["down"] == 1
    assert abs(a["feedback"]["positive_rate"] - 0.5) < 1e-9
    assert a["leads"]["total"] == 1
    assert a["handoff_funnel"]["out_of_scope_queries"] == 1
    assert a["handoff_funnel"]["capture_rate"] == 1.0
    # top questions derived from feedback + leads
    qs = [q["question"] for q in a["top_questions"]]
    assert "capital of France?" in qs
    assert "quantum computing trends 2050?" in qs


def test_analytics_requires_admin(client):
    t = _mk(client, "noauth")
    r = client.get(f"/admin/analytics/{t['tenant_id']}")
    assert r.status_code == 403, r.text


def test_analytics_missing_tenant_404(client):
    r = client.get("/admin/analytics/nope", headers=ADMIN)
    assert r.status_code == 404, r.text


def test_analytics_csv_export(client):
    t = _mk(client, "csv")
    key = t["api_key"]
    auth = {"Authorization": f"Bearer {key}"}
    tid = t["tenant_id"]
    _ingest(client, tid, auth, "Paris is the capital of France. " * 10)
    client.post(f"{V}/{tid}/query", headers=auth,
                json={"question": "capital of France?", "generate": False, "top_k": 2})
    client.post(f"{V}/{tid}/feedback", headers=auth, json={"rating": "up", "question": "capital of France?"})

    r = client.get(f"/admin/analytics/{tid}?fmt=csv", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv"), r.headers
    body = r.text
    assert body.startswith("date,"), body[:80]
    assert "feedback_up" in body and "leads_total" in body
    # at least a header + one data row
    assert len(body.strip().splitlines()) >= 2
