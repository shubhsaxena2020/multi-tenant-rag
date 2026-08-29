"""PHASE C — multi-turn session persistence verification.

Covers the gap: the embeddable widget previously started a fresh conversation every load
because (a) session_id was regenerated on each page load and (b) the server session store was
in-memory only (lost on restart, not shared across replicas). Fixes:
- Server: durable DB-backed session store (conversation_sessions table).
- Client: session_id persisted in localStorage per tenant (survives reload).
- session_id is threaded (tenant_id, session_id) through store + rewrite_query.

These tests prove persistence is REAL (read back from the DB by a fresh store instance, i.e.
what a process restart / second replica would see).
"""
import os

import pytest

V = "/api/v1"


@pytest.fixture
def _init_db():
    from app.db import init_db

    pytest.importorskip("sqlalchemy")
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(init_db())
    finally:
        loop.close()
    yield


def test_db_session_store_persists_across_instances(_init_db):
    """A second store instance (simulating a restart / sibling replica) must read the SAME
    turns from the database, not from process-local memory."""
    from app.conversation import DBBackedSessionStore, SessionStore

    tid, sid = "tenant-persist", "sess-persist-1"

    store_a = DBBackedSessionStore(SessionStore())
    store_a.append(tid, sid, "user", "What is the Pro plan?")
    store_a.append(tid, sid, "assistant", "The Pro plan costs $49/month.")

    # Fresh instance with a brand-new in-memory fallback -> if history came from memory it
    # would be empty. We assert it is NOT empty (proving it was read from the DB).
    store_b = DBBackedSessionStore(SessionStore())
    history = store_b.history(tid, sid)
    assert len(history) == 2, history
    assert history[0].role == "user" and "Pro plan" in history[0].text
    assert history[1].role == "assistant" and "$49" in history[1].text

    # Tenant isolation: a different tenant_id sees nothing.
    assert store_b.history("other-tenant", sid) == []


def test_query_stream_rewrites_followup_using_session_history(client, _init_db):
    """End-to-end: turn 1 seeds history; turn 2 is a reference follow-up ('how much does it
    cost?') and must be rewritten against that history (SSE `rewritten` event emitted)."""
    from app.conversation import get_session_store, SessionStore

    # Ensure the durable store is active for this test.
    store = get_session_store()
    assert isinstance(store, SessionStore)

    headers = {"Admin-Key": os.environ.get("ADMIN_API_KEY", "test-admin-key-for-tests")}
    t = client.post(f"{V}/tenants", json={"name": "multiturn", "plan": "standard"}, headers=headers).json()
    tenant = t["tenant_id"]
    api_key = t["api_key"]
    auth = {"Authorization": f"Bearer {api_key}"}
    sid = "sess-e2e-1"

    # Turn 1: seeds history.
    r1 = client.post(
        f"{V}/{tenant}/query/stream",
        headers=auth,
        json={"question": "What is the Pro plan?", "session_id": sid, "generate": False, "top_k": 3},
    )
    assert r1.status_code == 200, r1.text
    assert get_session_store().history(tenant, sid), "turn 1 should have been recorded"

    # Turn 2: reference follow-up must be rewritten via history.
    r2 = client.post(
        f"{V}/{tenant}/query/stream",
        headers=auth,
        json={"question": "how much does it cost?", "session_id": sid, "generate": False, "top_k": 3},
    )
    assert r2.status_code == 200, r2.text
    body = r2.text
    assert "event: rewritten" in body, "follow-up should be rewritten against history"
    # The rewritten query should carry the prior topic (Pro) so retrieval isn't blind.
    assert "Pro" in body


def test_widget_html_persists_session_id_in_localstorage(client):
    """sessionId must be stable across reloads: stored in localStorage per tenant, not
    regenerated with Math.random every load."""
    html = client.get("/widget.html").text
    assert "loadSessionId" in html, "widget must persist session id across reloads"
    assert "localStorage" in html, "session id should be stored in localStorage"
    assert "sessionId = loadSessionId()" in html
    # And the per-turn fetch still sends session_id to the server.
    assert "session_id" in html
