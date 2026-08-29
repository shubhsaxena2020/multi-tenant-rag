"""PHASE D — human-handoff / lead capture for out-of-scope queries (issue #33).

Real verification against the in-process app (TestClient):
- POST /api/v1/{tenant}/handoff (tenant secret key) captures a lead with a valid email or phone
  and returns {id}; missing contact -> 422; malformed email -> 422.
- GET /admin/leads/{tenant} (admin-only, fail-closed) returns a summary + recent leads; ?fmt=csv
  yields a real CSV attachment; missing tenant -> 404; no admin key -> 403.
- End-to-end trigger check: an off-topic question to /query/stream yields a `done` SSE event with
  out_of_scope=true (the signal the widget uses to show the handoff card).
"""
import os

from app.main import app

V = "/api/v1"
ADMIN = {"Admin-Key": os.environ.get("ADMIN_API_KEY", "test-admin-key-for-tests")}


def _mk(client, name="handoff"):
    r = client.post(f"{V}/tenants", json={"name": name, "plan": "standard"}, headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()


def test_handoff_capture_with_email(client):
    t = _mk(client)
    key = t["api_key"]
    auth = {"Authorization": f"Bearer {key}"}
    r = client.post(f"{V}/{t['tenant_id']}/handoff", headers=auth, json={
        "question": "Do you offer on-prem deployment?", "email": "lead@example.com",
        "name": "Jane", "session_id": "s1"})
    assert r.status_code == 200, r.text
    assert isinstance(r.json()["id"], int) and r.json()["id"] > 0


def test_handoff_capture_phone_only(client):
    t = _mk(client, "phone")
    key = t["api_key"]
    auth = {"Authorization": f"Bearer {key}"}
    r = client.post(f"{V}/{t['tenant_id']}/handoff", headers=auth, json={
        "question": "pricing?", "phone": "+1 555 123 4567"})
    assert r.status_code == 200, r.text


def test_handoff_requires_contact(client):
    t = _mk(client, "nocontact")
    key = t["api_key"]
    auth = {"Authorization": f"Bearer {key}"}
    r = client.post(f"{V}/{t['tenant_id']}/handoff", headers=auth,
                    json={"question": "x?"})  # no email/phone
    assert r.status_code == 422, r.text


def test_handoff_bad_email(client):
    t = _mk(client, "bademail")
    key = t["api_key"]
    auth = {"Authorization": f"Bearer {key}"}
    r = client.post(f"{V}/{t['tenant_id']}/handoff", headers=auth,
                    json={"question": "x?", "email": "not-an-email"})
    assert r.status_code == 422, r.text


def test_admin_leads_report(client):
    t = _mk(client, "report")
    key = t["api_key"]
    auth = {"Authorization": f"Bearer {key}"}
    for email in ("a@example.com", "b@example.com"):
        r = client.post(f"{V}/{t['tenant_id']}/handoff", headers=auth,
                        json={"question": "q", "email": email})
        assert r.status_code == 200, r.text

    r = client.get(f"/admin/leads/{t['tenant_id']}", headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["summary"]["total"] == 2, body
    assert body["summary"]["with_email"] == 2
    assert len(body["entries"]) == 2


def test_admin_leads_csv(client):
    t = _mk(client, "csv")
    key = t["api_key"]
    auth = {"Authorization": f"Bearer {key}"}
    client.post(f"{V}/{t['tenant_id']}/handoff", headers=auth,
                json={"question": "q", "email": "lead@example.com", "name": "Jo"})

    r = client.get(f"/admin/leads/{t['tenant_id']}?fmt=csv", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv"), r.headers
    assert "lead@example.com" in r.text and "Jo" in r.text


def test_admin_leads_requires_admin(client):
    t = _mk(client, "noauth")
    r = client.get(f"/admin/leads/{t['tenant_id']}")
    assert r.status_code == 403, r.text


def test_admin_leads_missing_tenant_404(client):
    r = client.get("/admin/leads/nope", headers=ADMIN)
    assert r.status_code == 404, r.text


def test_out_of_scope_triggers_handoff_signal(client):
    """The widget shows the handoff card when the SSE `done` event has out_of_scope=true.
    Verify the server actually emits that signal for an off-topic question."""
    t = _mk(client, "oostopic")
    key = t["api_key"]
    auth = {"Authorization": f"Bearer {key}"}
    # no documents ingested -> any question is out of scope
    with client.stream("POST", f"{V}/{t['tenant_id']}/query/stream",
                        headers=auth, json={"question": "quantum computing trends 2050?", "generate": True}) as resp:
        assert resp.status_code == 200, resp.status_code
        raw = ""
        for chunk in resp.iter_text():
            raw += chunk
    # find the done event payload
    import re, json
    m = re.search(r"event: done\s*\ndata: (\{.*?\})\s*\n", raw, re.S)
    assert m, raw[:500]
    data = json.loads(m.group(1))
    assert data.get("out_of_scope") is True, data
