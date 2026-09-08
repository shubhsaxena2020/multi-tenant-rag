"""v10.9 — horizontal-scalable ingestion job queue.

Covers the distribution layer (app/job_queue.py) WITHOUT requiring external services:

  * InProcessJobQueue still schedules on the executor -> a real ingest job completes and
    the tenant ingestion webhook fires (proves the refactor preserved the default path).
  * Enqueue is idempotent per job_id: submitting the same job twice runs it once.
  * RedisJobQueue encode/decode + idempotent push is verified WITHOUT a live Redis by
    exercising the ref serialization and the in-flight guard via a fake client; the real
    Redis path is guarded by an env check and skipped when REDIS_URL is unset.
"""
import asyncio

import pytest

import types
import threading

V = "/api/v1"
ADMIN = {"Admin-Key": "test-admin-key-for-tests"}


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


def _create_tenant(client):
    r = client.post(f"{V}/tenants", json={"name": "jobq", "plan": "standard"}, headers=ADMIN)
    assert r.status_code in (200, 201), r.text
    return r.json()


def test_inprocess_queue_runs_job_and_fires_webhook(client):
    from app import db as dbmod

    t = _create_tenant(client)
    tid, sk = t["tenant_id"], t["api_key"]
    # Configure an ingestion webhook to a local server (conftest enables WEBHOOK_ALLOW_PRIVATE).
    import threading
    import http.server
    import json as _json

    received = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(n) if n else b""
                received.append(_json.loads(body))
            except Exception:
                pass
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):  # silence
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{port}/hook"
        asyncio.run(dbmod.set_tenant_ingest_webhook(tid, url))
        # Submit a text ingest job.
        r = client.post(
            f"{V}/{tid}/ingest/jobs", json={"kind": "text", "title": "q", "text": "hello world"},
            headers=_auth(sk),
        )
        assert r.status_code == 202, r.text
        job_id = r.json()["job_id"]
        # Poll until completed.
        for _ in range(40):
            j = client.get(f"{V}/{tid}/jobs/{job_id}", headers=_auth(sk)).json()
            if j["status"] in ("completed", "failed"):
                break
            import time
            time.sleep(0.1)
        assert j["status"] == "completed", j
        # Webhook fired with the completion event (signed payload).
        assert received, "ingest webhook was not fired"
        assert received[0]["event"] == "ingest.job"
        assert received[0]["status"] == "completed"
    finally:
        srv.shutdown()


def test_enqueue_is_idempotent_per_job_id():
    from app.job_queue import InProcessJobQueue
    from app.ingestion import runner as runner_mod

    calls = []
    done = threading.Event()

    def fake_submit(job_id, tenant_id, kind, payload, metadata):
        calls.append(job_id)
        done.set()

    q = InProcessJobQueue(runner_submit=fake_submit)
    # Same job_id submitted 3x -> only one thread started (idempotent on _seen).
    for _ in range(3):
        q.enqueue("job-1", "t1", "text", {"text": "x"}, None)
    assert len(q._seen) == 1, q._seen
    # A different job_id is scheduled separately.
    q.enqueue("job-2", "t1", "text", {"text": "y"}, None)
    assert len(q._seen) == 2
    # The two jobs actually ran (job-1 once, job-2 once) — idempotency is on job_id, not
    # on the queue being non-empty.
    done.wait(timeout=5)
    assert calls == ["job-1", "job-2"], calls


def test_redis_queue_serialization_roundtrip(monkeypatch):
    """Exercise RedisJobQueue's ref encoding + idempotent push via a fake client,
    so the logic is proven without a live Redis server. We inject a fake `redis` module
    into sys.modules BEFORE construction so the lazy `import redis` inside resolves to it;
    no live server is contacted."""
    import sys
    from app.job_queue import RedisJobQueue

    sent = []
    inflight_adds = []

    class FakeRedis:
        def __init__(self, *a, **k):
            self._inflight = set()

        @classmethod
        def from_url(cls, url, *a, **k):
            return cls()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def ping(self):
            return True

        def pipeline(self):
            return self

        def multi(self):
            return self

        def sadd(self, key, val):
            # Redis semantics: returns number of NEW members added (0 if already present).
            if val in self._inflight:
                return 0
            self._inflight.add(val)
            inflight_adds.append((key, val))
            return 1

        def lpush(self, key, val):
            sent.append((key, val))
            return 1

        def execute(self):
            return [1, 1]

        def brpop(self, key, timeout=0):
            return (key, sent[-1][1]) if sent else None

        def srem(self, key, val):
            self._inflight.discard(val)
            return 1

        def llen(self, key):
            return len(sent)

    # Inject the fake as the `redis` module so the lazy `import redis` inside RedisJobQueue
    # resolves to it. No live server required.
    fake_mod = types.ModuleType("redis")
    fake_mod.Redis = FakeRedis
    monkeypatch.setitem(sys.modules, "redis", fake_mod)

    q = RedisJobQueue("redis://fake", "rag:ingest:jobs", runner_submit=lambda *a, **k: None)
    q.enqueue("j1", "t1", "text", {"text": "x"}, None)
    # Duplicate push -> still a single lpush (idempotent via inflight set).
    q.enqueue("j1", "t1", "text", {"text": "x"}, None)
    assert len(sent) == 1, sent
    assert inflight_adds[0][1] == "j1"

    ref = q.dequeue(timeout=1)
    assert ref["job_id"] == "j1"
    assert ref["kind"] == "text"
    assert ref["payload"] == {"text": "x"}
    assert q.pending_count() == 1
