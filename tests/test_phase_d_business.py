"""PHASE D — business value verification.

D.1 thumbs up/down feedback (publishable key allowed, isolated per tenant)
D.2 human-handoff / lead capture (secret-key only, stored, best-effort webhook)
D.3 per-tenant custom system prompt / persona (isolated, non-destructive)
"""
import asyncio
import time

import pytest

V = "/api/v1"


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


def _create_and_keep(client, name="acme"):
    headers = {"Admin-Key": "test-admin-key-for-tests"}
    r = client.post(f"{V}/tenants", json={"name": name, "plan": "standard"}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()  # holds api_key (secret rk_)


# --------------------------------------------------------------------------
# D.1 feedback
# --------------------------------------------------------------------------

def test_feedback_up_down_roundtrip(client):
    t = _create_and_keep(client)
    auth = _auth(t["api_key"])
    r = client.post(f"{V}/acme/feedback", headers=auth,
                    json={"rating": "up", "question": "what is pricing?"})
    assert r.status_code == 201, r.text
    assert r.json()["rating"] == "up"
    r2 = client.post(f"{V}/acme/feedback", headers=auth, json={"rating": "down"})
    assert r2.status_code == 201, r2.text

    lst = client.get(f"{V}/acme/feedback", headers=auth).json()
    assert len(lst) == 2
    ratings = {x["rating"] for x in lst}
    assert ratings == {"up", "down"}


def test_feedback_rejects_bad_rating(client):
    t = _create_and_keep(client)
    auth = _auth(t["api_key"])
    r = client.post(f"{V}/acme/feedback", headers=auth, json={"rating": "sideways"})
    assert r.status_code == 422, r.text


def test_feedback_accepts_publishable_key(client):
    """The widget only has a publishable pk_ key; feedback must work with it."""
    t = _create_and_keep(client)
    # mint a publishable key using the secret key
    pk = client.post(f"{V}/acme/keys/publishable", headers=_auth(t["api_key"])).json()["api_key"]
    assert pk.startswith("pk_")
    r = client.post(f"{V}/acme/feedback", headers=_auth(pk), json={"rating": "up"})
    assert r.status_code == 201, r.text


def test_feedback_is_tenant_scoped(client):
    ta = _create_and_keep(client, name="tenantA")
    tb = _create_and_keep(client, name="tenantB")
    au = _auth(ta["api_key"])
    bu = _auth(tb["api_key"])
    client.post(f"{V}/tenantA/feedback", headers=au, json={"rating": "up", "question": "A secret"})
    lst_b = client.get(f"{V}/tenantB/feedback", headers=bu).json()
    assert lst_b == [], "tenant B must not see tenant A's feedback"


# --------------------------------------------------------------------------
# D.2 lead capture
# --------------------------------------------------------------------------

def test_lead_capture_stored_and_secret_only(client):
    t = _create_and_keep(client)
    sk = t["api_key"]
    pk = client.post(f"{V}/acme/keys/publishable", headers=_auth(sk)).json()["api_key"]
    # publishable key must be REJECTED from lead capture (PII)
    bad = client.post(f"{V}/acme/lead", headers=_auth(pk),
                      json={"name": "Eve", "email": "eve@example.com", "question": "help"})
    assert bad.status_code == 403, bad.text
    # secret key works
    ok = client.post(f"{V}/acme/lead", headers=_auth(sk),
                    json={"name": "Bob", "email": "bob@example.com", "question": "out of scope"})
    assert ok.status_code == 201, ok.text
    leads = client.get(f"{V}/acme/leads", headers=_auth(sk)).json()
    assert len(leads) == 1
    assert leads[0]["email"] == "bob@example.com"


def test_lead_fires_webhook_best_effort(client, monkeypatch):
    """When a lead webhook is configured, a POST is fired; storage is the source of truth
    and a webhook failure must NOT fail the inbound capture."""
    import app.webhook as wh

    t = _create_and_keep(client)
    sk = t["api_key"]
    # configure webhook
    cfg = client.post(f"{V}/acme/config", headers=_auth(sk),
                      json={"lead_webhook_url": "https://hooks.example.com/lead"})
    assert cfg.status_code == 200, cfg.text

    # Drive the dispatch helper directly against a real local server to prove it POSTs
    # the right payload and that a non-2xx / failure is swallowed (returns False, no raise).
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    captured = {}

    class _H(BaseHTTPRequestHandler):
        def do_POST(self):
            ln = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(ln)
            captured["path"] = self.path
            captured["body"] = body.decode()
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    port = srv.server_address[1]
    import threading
    t_worker = threading.Thread(target=srv.serve_forever, daemon=True)
    t_worker.start()
    try:
        ok = wh.dispatch_lead_webhook(
            f"http://127.0.0.1:{port}/lead",
            {"lead_id": "lead_x", "email": "c@example.com"},
            t["tenant_id"],
        )
        assert ok is True
        assert captured["path"] == "/lead"
        import json as _json
        assert _json.loads(captured["body"])["email"] == "c@example.com"

        # A failing webhook (bad host) must return False, never raise.
        bad = wh.dispatch_lead_webhook("http://127.0.0.1:1/lead", {"x": 1}, t["tenant_id"])
        assert bad is False
    finally:
        srv.shutdown()

    # The inbound lead capture still stores the row regardless of webhook outcome.
    r = client.post(f"{V}/acme/lead", headers=_auth(sk),
                    json={"name": "Carol", "email": "c@example.com", "question": "quote?"})
    assert r.status_code == 201, r.text
    assert client.get(f"{V}/acme/leads", headers=_auth(sk)).json()[0]["email"] == "c@example.com"


# --------------------------------------------------------------------------
# D.3 persona / system prompt
# --------------------------------------------------------------------------

def test_persona_set_get_isolated(client):
    ta = _create_and_keep(client, name="tenantA")
    tb = _create_and_keep(client, name="tenantB")
    au = _auth(ta["api_key"])
    bu = _auth(tb["api_key"])
    persona_a = "You are Acme's friendly support bot. Always cite a source."
    r = client.post(f"{V}/tenantA/config", headers=au, json={"system_prompt": persona_a})
    assert r.status_code == 200, r.text
    # B gets default (empty), not A's persona.
    cfg_b = client.get(f"{V}/tenantB/config", headers=bu).json()
    assert cfg_b["system_prompt"] == "", "tenant B must not see tenant A's persona"
    cfg_a = client.get(f"{V}/tenantA/config", headers=au).json()
    assert cfg_a["system_prompt"] == persona_a
    assert cfg_a["lead_webhook_configured"] is False


def test_persona_rejects_non_http_webhook(client):
    t = _create_and_keep(client)
    auth = _auth(t["api_key"])
    r = client.post(f"{V}/acme/config", headers=auth, json={"lead_webhook_url": "file:///etc/passwd"})
    assert r.status_code == 422, r.text
