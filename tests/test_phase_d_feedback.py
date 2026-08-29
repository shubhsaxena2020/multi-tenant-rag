"""PHASE D — thumbs up/down feedback capture (issue #29).

Real verification against the in-process app (TestClient):
- POST /api/v1/{tenant}/feedback (tenant secret key) captures 'up'/'down' (+ question/answer/
  session_id) and returns an id; an invalid rating is rejected with 422.
- GET /admin/feedback/{tenant} (admin-only, fail-closed) returns a summary (up/down/total/
  positive_rate) + recent entries; ?fmt=csv yields a real CSV attachment.
- Admin gate (403 without Admin-Key) and missing-tenant 404 are enforced.
"""
import os

from app.main import app

V = "/api/v1"
ADMIN = {"Admin-Key": os.environ.get("ADMIN_API_KEY", "test-admin-key-for-tests")}


def _mk(client, name="fb"):
    r = client.post(f"{V}/tenants", json={"name": name, "plan": "standard"}, headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()


def test_feedback_capture(client):
    t = _mk(client)
    key = t["api_key"]
    tid = t["tenant_id"]
    auth = {"Authorization": f"Bearer {key}"}

    r = client.post(f"{V}/{tid}/feedback", headers=auth, json={
        "rating": "up", "question": "capital of France?", "answer": "Paris.",
        "session_id": "sess-1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["rating"] == "up"
    assert isinstance(body["id"], int) and body["id"] > 0

    r2 = client.post(f"{V}/{tid}/feedback", headers=auth, json={"rating": "down"})
    assert r2.status_code == 200, r2.text


def test_invalid_rating_rejected(client):
    t = _mk(client)
    key = t["api_key"]
    tid = t["tenant_id"]
    auth = {"Authorization": f"Bearer {key}"}
    r = client.post(f"{V}/{tid}/feedback", headers=auth, json={"rating": "sideways"})
    assert r.status_code == 422, r.text  # FeedbackIn.Literal enforces up/down


def test_admin_feedback_report(client):
    t = _mk(client, "report")
    key = t["api_key"]
    tid = t["tenant_id"]
    auth = {"Authorization": f"Bearer {key}"}
    for rating in ("up", "up", "down"):
        r = client.post(f"{V}/{tid}/feedback", headers=auth,
                        json={"rating": rating, "question": "q", "answer": "a"})
        assert r.status_code == 200, r.text

    r = client.get(f"/admin/feedback/{tid}", headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["summary"]["up"] == 2, body
    assert body["summary"]["down"] == 1, body
    assert body["summary"]["total"] == 3
    # positive_rate = 2/3
    assert abs(body["summary"]["positive_rate"] - (2 / 3)) < 1e-9, body
    assert len(body["entries"]) == 3
    # the captured question/answer round-trip
    assert any(e["question"] == "q" and e["answer"] == "a" for e in body["entries"])


def test_admin_feedback_csv(client):
    t = _mk(client, "csv")
    key = t["api_key"]
    tid = t["tenant_id"]
    auth = {"Authorization": f"Bearer {key}"}
    client.post(f"{V}/{tid}/feedback", headers=auth, json={"rating": "up", "question": "q", "answer": "a"})

    r = client.get(f"/admin/feedback/{tid}?fmt=csv", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv"), r.headers
    text = r.text
    assert text.startswith("id,rating,session_id,message_id,question,answer,comment,ts")
    assert "up" in text and "q" in text


def test_admin_feedback_requires_admin_key(client):
    t = _mk(client, "noauth")
    tid = t["tenant_id"]
    r = client.get(f"/admin/feedback/{tid}")
    assert r.status_code == 403, r.text


def test_admin_feedback_missing_tenant_404(client):
    r = client.get("/admin/feedback/does_not_exist", headers=ADMIN)
    assert r.status_code == 404, r.text
