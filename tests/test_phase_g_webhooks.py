"""v10.6 — SSRF-guarded, HMAC-signed outbound webhooks.

Covers BOTH the lead webhook (PHASE D.2) and the new generic ingestion webhook:

  * SSRF guard rejects tenant-supplied webhook URLs that resolve to internal /
    loopback / link-local / cloud-metadata (169.254.169.254) addresses or use a
    non-http(s) scheme / non-standard port — at CONFIG time (fail-closed).
  * HMAC signature header is present when a master key is configured.
  * Ingestion webhook fires on job completion (and failure) with no effect on the job.
  * Webhook failures never fail the inbound capture / ingestion.

Local servers in these tests use WEBHOOK_ALLOW_PRIVATE=1 so loopback egress is
permitted ONLY for the harness (production keeps it off, see config.webhook_allow_private).
"""
import asyncio
import os

import pytest

V = "/api/v1"
ADMIN = {"Admin-Key": "test-admin-key-for-tests"}


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


def _create_tenant(client):
    r = client.post(f"{V}/tenants", json={"name": "wh", "plan": "standard"}, headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# SSRF guard on tenant-supplied webhook URLs (the headline security fix)
# ---------------------------------------------------------------------------

def test_config_rejects_metadata_ip_webhook(client):
    """A tenant must NOT be able to aim a webhook at the cloud IMDS endpoint."""
    t = _create_tenant(client)
    r = client.post(
        f"{V}/{t['tenant_id']}/config", headers=_auth(t["api_key"]),
        json={"ingest_webhook_url": "http://169.254.169.254/latest/meta-data/"},
    )
    assert r.status_code == 422, r.text
    assert "SSRF" in r.text


def test_config_rejects_localhost_webhook(client):
    """Reject loopback targets (would let a tenant probe our internal network)."""
    t = _create_tenant(client)
    r = client.post(
        f"{V}/{t['tenant_id']}/config", headers=_auth(t["api_key"]),
        json={"lead_webhook_url": "http://localhost:8080/hook"},
    )
    assert r.status_code == 422, r.text
    assert "SSRF" in r.text


def test_config_rejects_non_http_scheme(client):
    """file:// and other non-http(s) schemes are refused."""
    t = _create_tenant(client)
    r = client.post(
        f"{V}/{t['tenant_id']}/config", headers=_auth(t["api_key"]),
        json={"lead_webhook_url": "file:///etc/passwd"},
    )
    assert r.status_code == 422, r.text


def test_config_rejects_nonstandard_port(client):
    """Only ports 80/443 are permitted egress ports."""
    t = _create_tenant(client)
    r = client.post(
        f"{V}/{t['tenant_id']}/config", headers=_auth(t["api_key"]),
        json={"ingest_webhook_url": "http://example.com:8080/hook"},
    )
    assert r.status_code == 422, r.text


def test_config_accepts_public_https_webhook(client):
    """A genuinely public https URL is accepted and reflected in config."""
    t = _create_tenant(client)
    url = "https://hooks.example.com/ingest"
    r = client.post(
        f"{V}/{t['tenant_id']}/config", headers=_auth(t["api_key"]),
        json={"ingest_webhook_url": url},
    )
    assert r.status_code == 200, r.text
    cfg = client.get(f"{V}/{t['tenant_id']}/config", headers=_auth(t["api_key"])).json()
    assert cfg["ingest_webhook_configured"] is True


# ---------------------------------------------------------------------------
# HMAC signing
# ---------------------------------------------------------------------------

def test_webhook_payload_is_signed(client):
    """When a master key is set, outbound webhooks carry an HMAC signature header."""
    import app.webhook as wh

    body = wh._json_dumps({"event": "x", "job_id": "j1", "tenant_id": "t1"})
    headers = wh._build_headers("t1", body.encode())
    assert wh.SIG_HEADER in headers
    assert headers[wh.TENANT_HEADER] == "t1"
    # And the signature verifies against the master key.
    key = wh._signing_key()
    assert key is not None
    assert wh.sign_payload(key, body.encode()) == headers[wh.SIG_HEADER]


# ---------------------------------------------------------------------------
# Ingestion webhook fires on job completion (no effect on the job)
# ---------------------------------------------------------------------------

def test_ingest_webhook_fires_on_job_completion(client):
    """A tenant's ingestion callback is POSTed when an async job finishes."""
    import app.webhook as wh
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import json as _json
    import threading

    t = _create_tenant(client)
    tid = t["tenant_id"]
    sk = t["api_key"]

    captured = {}

    class _H(BaseHTTPRequestHandler):
        def do_POST(self):
            ln = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(ln)
            captured["path"] = self.path
            captured["body"] = raw.decode()
            captured["sig"] = self.headers.get(wh.SIG_HEADER)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    port = srv.server_address[1]
    worker = threading.Thread(target=srv.serve_forever, daemon=True)
    worker.start()
    try:
        # Configure the ingestion webhook (loopback allowed by the fixture).
        cfg = client.post(
            f"{V}/{tid}/config", headers=_auth(sk),
            json={"ingest_webhook_url": f"http://127.0.0.1:{port}/ingest"},
        )
        assert cfg.status_code == 200, cfg.text

        # Kick off an async ingestion job (text).
        r = client.post(
            f"{V}/{tid}/ingest/jobs", headers=_auth(sk),
            json={"title": "doc", "text": "Acme support costs $20/month for Pro tier.",
                  "content_type": "text"},
        )
        assert r.status_code == 202, r.text
        job_id = r.json()["job_id"]

        # Poll until completed (the runner fires the webhook on completion).
        for _ in range(40):
            j = client.get(f"{V}/{tid}/jobs/{job_id}", headers=_auth(sk)).json()
            if j["status"] in ("completed", "failed"):
                break
            import time
            time.sleep(0.25)
        assert j["status"] == "completed", j

        # The webhook must have been called with a signed completion event.
        assert captured.get("body"), "ingestion webhook was never called"
        evt = _json.loads(captured["body"])
        assert evt["event"] == "ingest.job"
        assert evt["job_id"] == job_id
        assert evt["status"] == "completed"
        assert captured.get("sig"), "webhook payload was not HMAC-signed"
    finally:
        srv.shutdown()


# ---------------------------------------------------------------------------
# Best-effort: webhook failure never fails the primary path
# ---------------------------------------------------------------------------

def test_lead_capture_survives_webhook_failure(client):
    """Lead storage is the source of truth; a dead webhook must not fail the capture."""
    import app.webhook as wh

    t = _create_tenant(client)
    sk = t["api_key"]
    # Configure a webhook URL that points at nothing reachable.
    cfg = client.post(
        f"{V}/{t['tenant_id']}/config", headers=_auth(sk),
        json={"lead_webhook_url": "https://hooks.example.com/lead"},
    )
    assert cfg.status_code == 200, cfg.text

    # Drive the dispatch helper directly: a bad/blocked target returns False, never raises.
    ok = wh.dispatch_lead_webhook("http://127.0.0.1:1/lead", {"x": 1}, t["tenant_id"])
    assert ok is False

    # Inbound capture still stores the lead regardless.
    r = client.post(
        f"{V}/{t['tenant_id']}/lead", headers=_auth(sk),
        json={"name": "Eve", "email": "eve@example.com", "question": "quote?"},
    )
    assert r.status_code == 201, r.text
    leads = client.get(f"{V}/{t['tenant_id']}/leads", headers=_auth(sk)).json()
    assert leads[0]["email"] == "eve@example.com"


def test_lead_webhook_posts_payload_to_local_server(client):
    """End-to-end: lead webhook POSTs the right signed payload to the configured URL."""
    import app.webhook as wh
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import json as _json
    import threading

    t = _create_tenant(client)
    sk = t["api_key"]
    captured = {}

    class _H(BaseHTTPRequestHandler):
        def do_POST(self):
            ln = int(self.headers.get("Content-Length", "0"))
            captured["body"] = self.rfile.read(ln).decode()
            captured["sig"] = self.headers.get(wh.SIG_HEADER)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    port = srv.server_address[1]
    worker = threading.Thread(target=srv.serve_forever, daemon=True)
    worker.start()
    try:
        ok = wh.dispatch_lead_webhook(
            f"http://127.0.0.1:{port}/lead",
            {"lead_id": "lead_x", "email": "c@example.com"}, t["tenant_id"],
        )
        assert ok is True
        assert _json.loads(captured["body"])["email"] == "c@example.com"
        assert captured.get("sig"), "lead webhook not signed"
    finally:
        srv.shutdown()
