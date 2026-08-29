"""PHASE D — conversation memory UI: server session-history endpoint (issue #31).

The widget side (loading + rendering prior turns) is verified by JS parse + the endpoint
contract below. Server-side verification against the in-process app (TestClient):
- Running real /query calls with a session_id appends user+assistant turns to the store.
- GET /{tenant}/session/{session_id} (tenant secret key) returns the turns in order.
- No auth -> 403; tenant/path mismatch -> 403; unknown session -> empty turns (not 404).
"""
import os

from app.main import app

V = "/api/v1"
ADMIN = {"Admin-Key": os.environ.get("ADMIN_API_KEY", "test-admin-key-for-tests")}


def _mk(client, name="mem"):
    r = client.post(f"{V}/tenants", json={"name": name, "plan": "standard"}, headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()


def test_session_history_roundtrip(client):
    t = _mk(client)
    key = t["api_key"]
    tid = t["tenant_id"]
    auth = {"Authorization": f"Bearer {key}"}
    sid = "sess-memory-test"

    # ingest a doc so answers are generated
    client.post(f"{V}/{tid}/documents", headers=auth,
                json={"title": "cap", "content": "The capital of France is Paris. " * 8, "content_type": "text"})

    # two queries with the same session_id -> store appends user+assistant each time
    for q in ("capital of France?", "what about Germany?"):
        r = client.post(f"{V}/{tid}/query", headers=auth,
                        json={"question": q, "generate": False, "top_k": 2, "session_id": sid})
        assert r.status_code == 200, r.text

    r = client.get(f"{V}/{tid}/session/{sid}", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["session_id"] == sid
    turns = body["turns"]
    # 2 user + 2 assistant = 4 turns, alternating, user first
    assert len(turns) == 4, turns
    assert turns[0]["role"] == "user" and turns[0]["text"] == "capital of France?"
    assert turns[1]["role"] == "assistant"
    assert turns[2]["role"] == "user" and turns[2]["text"] == "what about Germany?"
    assert turns[3]["role"] == "assistant"


def test_session_history_requires_auth(client):
    t = _mk(client, "noauth")
    tid = t["tenant_id"]
    # No Authorization header -> require_secret_key rejects with 401 (fail-closed).
    r = client.get(f"{V}/{tid}/session/whatever")
    assert r.status_code == 401, r.text


def test_session_history_tenant_mismatch_403(client):
    a = _mk(client, "A")
    b = _mk(client, "B")
    authA = {"Authorization": f"Bearer {a['api_key']}"}
    # key A used against path tenant B -> mismatch
    r = client.get(f"{V}/{b['tenant_id']}/session/sid-x", headers=authA)
    assert r.status_code == 403, r.text


def test_unknown_session_returns_empty_turns(client):
    t = _mk(client, "unknown")
    auth = {"Authorization": f"Bearer {t['api_key']}"}
    r = client.get(f"{V}/{t['tenant_id']}/session/never-created", headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["turns"] == [], r.json()
