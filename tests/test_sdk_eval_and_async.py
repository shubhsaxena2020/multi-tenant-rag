"""PHASE C — SDK eval-endpoint route fixes + async client (issue #25).

Real verification:
- Sync client: the eval methods were pointing at NON-EXISTENT server routes
  (PUT /eval-sets/{name}, POST /eval/{name}). We assert the fixed client now issues
  requests to the REAL routes (PUT /eval/set, POST /eval/run, POST /eval/quality,
  GET /eval/runs) by capturing the URL each method hits (no live server needed).
- Async client: full end-to-end against the in-process ASGI app via httpx.ASGITransport —
  creates a tenant, ingests a doc, uploads a golden set, runs retrieval eval + quality eval,
  lists runs, and streams a query. This exercises the real HTTP path through the real routes.
"""
import asyncio
import os

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from sdk import AsyncRagClient, RagClient

V = "/api/v1"
ADMIN = {"Admin-Key": os.environ.get("ADMIN_API_KEY", "test-admin-key-for-tests")}


# ---------------- sync client: route-target fixes ----------------
def test_sync_eval_routes_are_correct():
    """The core bug: previously these hit /eval-sets/{name} and /eval/{name} (404)."""
    captured = {}

    c = RagClient(base_url="https://rag.example.com/api/v1", api_key="rk_x")
    # Patch the low-level helpers so no network call happens; we just capture the path.
    # NOTE: the SDK builds paths RELATIVE to base_url (which already includes /api/v1).
    c._put = lambda path, body, **kw: captured.update(method="PUT", url=path) or {"ok": True}
    c._post = lambda path, body, **kw: captured.update(method="POST", url=path) or {"ok": True}
    c._get = lambda path, **kw: captured.update(method="GET", url=path) or [{"ok": True}]

    c.put_eval_set("acme", [{"question": "q"}])
    assert captured["method"] == "PUT"
    assert captured["url"] == "/acme/eval/set", captured["url"]

    c.run_eval("acme")
    assert captured["method"] == "POST"
    assert captured["url"] == "/acme/eval/run", captured["url"]

    c.run_eval_quality("acme")
    assert captured["method"] == "POST"
    assert captured["url"] == "/acme/eval/quality", captured["url"]

    c.eval_runs("acme")
    assert captured["method"] == "GET"
    assert captured["url"] == "/acme/eval/runs", captured["url"]


def _make_tenant(client):
    r = client.post(f"{V}/tenants", json={"name": "sdkasync", "plan": "standard"}, headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()


# ---------------- async client: real end-to-end ----------------
def test_async_client_eval_end_to_end():
    """Drive the async SDK through the real app (ASGI transport) and assert eval works."""
    async def go():
        transport = httpx.ASGITransport(app=app)
        async with AsyncRagClient(
            base_url="http://testserver/api/v1", api_key="rk_placeholder", transport=transport
        ) as c:
            # We need a real tenant + key. Create one via the sync TestClient, then use its key.
            with TestClient(app) as tc:
                t = _make_tenant(tc)
                key = t["api_key"]
                c.api_key = key
                tenant = t["tenant_id"]

                # ingest a doc
                await c.ingest_text(tenant, "capitals",
                                    "The capital of France is Paris. It is in Europe.",
                                    content_type="text")

                # golden set
                res = await c.put_eval_set(tenant, [
                    {"question": "capital of France?",
                     "relevant_texts": ["The capital of France is Paris."]},
                ])
                assert res.get("saved") == 1, res

                # retrieval eval
                rep = await c.run_eval(tenant, top_k=4)
                assert "hit_rate" in rep and "mrr" in rep, rep

                # quality eval
                q = await c.run_eval_quality(tenant, top_k=4, persist=True)
                assert "hit_rate" in q and "run_id" in q, q

                # runs history
                runs = await c.eval_runs(tenant)
                assert isinstance(runs, list) and len(runs) >= 1

                # async query stream
                events = []
                async for ev in c.query_stream(tenant, "capital of France?", generate=False, top_k=3):
                    events.append(ev)
                assert any(e["event"] == "sources" for e in events), events
                # every event has the {event, data} shape
                assert all("event" in e and "data" in e for e in events)
        return True

    assert asyncio.run(go()) is True


def test_async_client_requires_admin_key_for_tenant_create():
    async def go():
        transport = httpx.ASGITransport(app=app)
        async with AsyncRagClient(base_url="http://testserver/api/v1", api_key="rk_x",
                                  transport=transport) as c:
            with pytest.raises(ValueError):
                await c.create_tenant("no-admin")
        return True

    assert asyncio.run(go()) is True
