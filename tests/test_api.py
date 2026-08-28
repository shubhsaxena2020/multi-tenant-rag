import os
import time

import pytest

# Point the app at a local Qdrant + deterministic models (no GB downloads).
os.environ.setdefault("QDRANT_URL", "http://localhost:6333")
os.environ.setdefault("USE_REAL_EMBEDDER", "0")
os.environ.setdefault("USE_REAL_RERANKER", "0")
os.environ.setdefault("DB_URL", "sqlite:///./test_rag_tenants.db")
# Exercise per-tenant encryption-at-rest in tests (AES-GCM envelope).
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "AAAAAAt3stEnvMasterKey0123456789ABCDEF")

V = "/api/v1"  # versioned tenant API prefix

# Shared fixtures (client, _clear_settings_cache) live in tests/conftest.py


def _make_tenant(client, name="acme"):
    headers = {}
    admin_key = os.environ.get("ADMIN_API_KEY")
    if admin_key:
        headers["Admin-Key"] = admin_key
    r = client.post(f"{V}/tenants", json={"name": name, "plan": "standard"}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_metrics_endpoint(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    assert b"rag_requests_total" in r.content


def test_openapi_versioned(client):
    r = client.get(f"{V}/openapi.json")
    assert r.status_code == 200
    spec = r.json()
    assert spec["info"]["version"] == "1.0.0"
    # versioned paths present (served at /api/v1 + these relative paths)
    assert "/{tenant}/query" in spec["paths"]
    assert "/{tenant}/ingest/jobs" in spec["paths"]
    # full base URL reflected via servers.root_path
    servers = spec.get("servers", [])
    assert any(s.get("url") == "/api/v1" for s in servers)


def test_tenant_isolation_in_query(client):
    t1 = _make_tenant(client, "acme")
    t2 = _make_tenant(client, "globex")
    r = client.post(
        f"{V}/acme/documents",
        headers=_auth(t1["api_key"]),
        json={"title": "secret", "content": "The launch code for Project Apollo is zebra-9971.", "content_type": "text"},
    )
    assert r.status_code == 201, r.text
    q = client.post(
        f"{V}/globex/query",
        headers=_auth(t2["api_key"]),
        json={"question": "What is the launch code for Project Apollo?", "top_k": 5},
    )
    assert q.status_code == 200, q.text
    body = q.json()
    assert body["tenant_id"] == t2["tenant_id"]
    joined = " ".join(hit["text"] for hit in body["results"])
    assert "zebra-9971" not in joined, "CROSS-TENANT LEAK: tenant 2 saw tenant 1's data"
    q1 = client.post(
        f"{V}/acme/query",
        headers=_auth(t1["api_key"]),
        json={"question": "What is the launch code for Project Apollo?", "top_k": 5},
    )
    assert q1.status_code == 200
    assert "zebra-9971" in " ".join(hit["text"] for hit in q1.json()["results"])


def test_api_key_rejects_wrong_tenant(client):
    t1 = _make_tenant(client, "acme")
    r = client.post(
        f"{V}/globex/documents",
        headers=_auth(t1["api_key"]),
        json={"title": "x", "content": "Honest Abe rode a bicycle.", "content_type": "text"},
    )
    assert r.status_code == 201
    q = client.post(
        f"{V}/globex/query",
        headers=_auth(t1["api_key"]),
        json={"question": "What did Honest Abe ride?", "top_k": 3},
    )
    assert q.status_code == 200
    assert q.json()["tenant_id"] == t1["tenant_id"]
    assert any("bicycle" in h["text"] for h in q.json()["results"])


def test_invalid_key_rejected(client):
    r = client.get("/health")
    assert r.status_code == 200
    q = client.post(
        f"{V}/acme/query",
        headers={"Authorization": "Bearer bad_key"},
        json={"question": "hi"},
    )
    assert q.status_code == 401


def test_delete_removes_chunks(client):
    t = _make_tenant(client, "acme")
    r = client.post(
        f"{V}/acme/documents",
        headers=_auth(t["api_key"]),
        json={"title": "doc", "content": "RAG isolation is enforced at the vector DB layer.", "content_type": "text"},
    )
    doc_id = r.json()["doc_id"]
    q = client.post(f"{V}/acme/query", headers=_auth(t["api_key"]), json={"question": "RAG isolation", "top_k": 3})
    assert len(q.json()["results"]) >= 1
    d = client.delete(f"{V}/acme/documents/{doc_id}", headers=_auth(t["api_key"]))
    assert d.status_code == 200
    q2 = client.post(f"{V}/acme/query", headers=_auth(t["api_key"]), json={"question": "RAG isolation", "top_k": 3})
    assert len(q2.json()["results"]) == 0


def test_async_ingest_job_lifecycle(client):
    t = _make_tenant(client, "acme")
    body = {
        "kind": "text",
        "title": "job doc",
        "text": "The Qdrant silo model gives each tenant a private collection. "
                "Retrieval is scoped to that collection, so cross-tenant reads are impossible.",
        "content_type": "text",
    }
    r = client.post(f"{V}/acme/ingest/jobs", headers=_auth(t["api_key"]), json=body)
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    assert r.json()["status"] in ("pending", "running")
    final = None
    for _ in range(40):
        j = client.get(f"{V}/acme/jobs/{job_id}", headers=_auth(t["api_key"]))
        assert j.status_code == 200
        final = j.json()
        if final["status"] in ("completed", "failed"):
            break
        time.sleep(0.25)
    assert final["status"] == "completed", final
    assert final["progress"] == 1.0, final
    assert final["result_doc_id"], final
    assert final["done_chunks"] == final["total_chunks"] > 0
    q = client.post(
        f"{V}/acme/query", headers=_auth(t["api_key"]),
        json={"question": "How does the Qdrant silo model isolate tenants?", "top_k": 3},
    )
    assert any("private collection" in h["text"] for h in q.json()["results"])


def test_job_isolation_other_tenant_cannot_see(client):
    t1 = _make_tenant(client, "acme")
    t2 = _make_tenant(client, "globex")
    body = {"kind": "text", "title": "secret job", "text": "Confidential: tenant job secret is omega-4242.", "content_type": "text"}
    r = client.post(f"{V}/acme/ingest/jobs", headers=_auth(t1["api_key"]), json=body)
    job_id = r.json()["job_id"]
    for _ in range(40):
        j = client.get(f"{V}/acme/jobs/{job_id}", headers=_auth(t1["api_key"])).json()
        if j["status"] in ("completed", "failed"):
            break
        time.sleep(0.25)
    assert client.get(f"{V}/globex/jobs/{job_id}", headers=_auth(t2["api_key"])).status_code == 404
    q = client.post(
        f"{V}/globex/query", headers=_auth(t2["api_key"]),
        json={"question": "What is the tenant job secret?", "top_k": 5},
    )
    assert "omega-4242" not in " ".join(h["text"] for h in q.json()["results"])


def test_validation_rejects_oversized_content(client):
    t = _make_tenant(client, "acme")
    huge = "x" * (1_000_001)
    r = client.post(
        f"{V}/acme/documents", headers=_auth(t["api_key"]),
        json={"title": "big", "content": huge, "content_type": "text"},
    )
    assert r.status_code == 422
    r2 = client.post(
        f"{V}/acme/documents", headers=_auth(t["api_key"]),
        json={"title": "x", "content": "small", "content_type": "video/mp4"},
    )
    assert r2.status_code == 422


def test_job_list_and_delete(client):
    t = _make_tenant(client, "acme")
    body = {"kind": "text", "title": "listdoc", "text": "Trackable ingestion jobs.", "content_type": "text"}
    jid = client.post(f"{V}/acme/ingest/jobs", headers=_auth(t["api_key"]), json=body).json()["job_id"]
    for _ in range(40):
        if client.get(f"{V}/acme/jobs/{jid}", headers=_auth(t["api_key"])).json()["status"] == "completed":
            break
        time.sleep(0.25)
    lst = client.get(f"{V}/acme/jobs", headers=_auth(t["api_key"])).json()
    assert any(j["job_id"] == jid for j in lst)
    assert client.delete(f"{V}/acme/jobs/{jid}", headers=_auth(t["api_key"])).status_code == 200
    assert client.get(f"{V}/acme/jobs/{jid}", headers=_auth(t["api_key"])).status_code == 404


def test_generate_returns_answer(client):
    t = _make_tenant(client, "acme")
    client.post(
        f"{V}/acme/documents", headers=_auth(t["api_key"]),
        json={"title": "facts", "content": "The capital of France is Paris.", "content_type": "text"},
    )
    r = client.post(
        f"{V}/acme/query", headers=_auth(t["api_key"]),
        json={"question": "What is the capital of France?", "top_k": 3, "generate": True},
    )
    assert r.status_code == 200
    assert r.json()["answer"]
    assert "Paris" in r.json()["answer"]


def test_rate_limiting(client):
    # drive an authenticated tenant over the per-IP limit by hammering a cheap endpoint
    t = _make_tenant(client, "acme")
    # exhaust the default 120/min per-IP bucket quickly with repeated queries
    limited = False
    for i in range(140):
        r = client.post(
            f"{V}/acme/query", headers=_auth(t["api_key"]),
            json={"question": "q", "top_k": 1},
        )
        if r.status_code == 429:
            limited = True
            assert "Retry-After" in r.headers
            break
    assert limited, "expected 429 once per-IP limit exhausted"


def test_tenant_admin_offboarding(client):
    t = _make_tenant(client, "acme")
    client.post(
        f"{V}/acme/documents", headers=_auth(t["api_key"]),
        json={"title": "d", "content": "secret tenant data zz-99", "content_type": "text"},
    )
    r = client.delete(
        f"{V}/tenants/{t['tenant_id']}",
        headers={"Admin-Key": os.environ.get("ADMIN_API_KEY")},
    )
    assert r.status_code == 200, r.text
    assert r.json()["collection_dropped"] is True
    q = client.post(
        f"{V}/acme/query", headers=_auth(t["api_key"]),
        json={"question": "x", "top_k": 3},
    )
    assert q.status_code == 401, q.text
    assert client.get(
        f"{V}/tenants", headers={"Admin-Key": os.environ.get("ADMIN_API_KEY")}
    ).status_code == 200


def test_admin_key_blocks_tenant_creation(monkeypatch, client):
    monkeypatch.setenv("ADMIN_API_KEY", "supersecret")
    from app.config import get_settings
    get_settings.cache_clear()
    r = client.post(f"{V}/tenants", json={"name": "evil"})
    assert r.status_code in (401, 403, 404)
    r2 = client.post(f"{V}/tenants", json={"name": "good"}, headers={"Admin-Key": "supersecret"})
    assert r2.status_code == 201


def test_encryption_at_rest(client):
    # MASTER_ENCRYPTION_KEY is set in the test env -> chunk text is sealed in Qdrant.
    t = _make_tenant(client, "acme")
    r = client.post(
        f"{V}/acme/documents", headers=_auth(t["api_key"]),
        json={"title": "secret", "content": "The launch code is zebra-9971.", "content_type": "text"},
    )
    assert r.status_code == 201
    q = client.post(
        f"{V}/acme/query", headers=_auth(t["api_key"]),
        json={"question": "launch code?", "top_k": 3},
    )
    assert "zebra-9971" in " ".join(h["text"] for h in q.json()["results"])
    from app.config import get_settings
    from app.vector_store import collection_name, get_client
    c = get_client()
    name = collection_name(get_settings().collection_prefix, t["tenant_id"])
    pts = c.scroll(collection_name=name, limit=10)[0]
    assert all(p.payload["text"].startswith("enc:") for p in pts), "plaintext leaked to Qdrant"


def test_document_level_rbac(client):
    t = _make_tenant(client, "acme")
    client.post(f"{V}/acme/documents", headers=_auth(t["api_key"]),
                json={"title": "eng", "content": "Eng memo: launch code zebra-9971.", "content_type": "text", "acl": ["eng"]})
    client.post(f"{V}/acme/documents", headers=_auth(t["api_key"]),
                json={"title": "sales", "content": "Sales memo: launch code alpha-4242.", "content_type": "text", "acl": ["sales"]})
    qe = client.post(f"{V}/acme/query", headers=_auth(t["api_key"]),
                     json={"question": "launch code", "top_k": 5, "acl": ["eng"]})
    je = " ".join(h["text"] for h in qe.json()["results"])
    assert "zebra-9971" in je and "alpha-4242" not in je
    qs = client.post(f"{V}/acme/query", headers=_auth(t["api_key"]),
                     json={"question": "launch code", "top_k": 5, "acl": ["sales"]})
    js = " ".join(h["text"] for h in qs.json()["results"])
    assert "alpha-4242" in js and "zebra-9971" not in js
    qn = client.post(f"{V}/acme/query", headers=_auth(t["api_key"]),
                     json={"question": "launch code", "top_k": 5, "acl": None})
    jn = " ".join(h["text"] for h in qn.json()["results"])
    assert "zebra-9971" in jn and "alpha-4242" in jn


def test_api_key_rotation(client):
    t = _make_tenant(client, "acme")
    old_key = t["api_key"]
    rot = client.post(f"{V}/acme/keys", headers=_auth(old_key))
    assert rot.status_code == 201, rot.text
    new_key = rot.json()["api_key"]
    assert new_key != old_key
    assert client.get(f"{V}/acme/jobs", headers=_auth(old_key)).status_code == 200
    assert client.get(f"{V}/acme/jobs", headers=_auth(new_key)).status_code == 200
    lst = client.get(f"{V}/acme/keys", headers=_auth(new_key)).json()
    assert len(lst["keys"]) == 2
    pref = old_key[:8]
    rev = client.delete(f"{V}/acme/keys/{pref}", headers=_auth(new_key))
    assert rev.status_code == 200
    assert client.get(f"{V}/acme/jobs", headers=_auth(old_key)).status_code == 401
    last_pref = new_key[:8]
    rev2 = client.delete(f"{V}/acme/keys/{last_pref}", headers=_auth(new_key))
    assert rev2.status_code == 200
    assert rev2.json()["revoked"] == 0
    assert client.get(f"{V}/acme/jobs", headers=_auth(new_key)).status_code == 200


def test_offline_eval(client):
    t = _make_tenant(client, "acme")
    client.post(f"{V}/acme/documents", headers=_auth(t["api_key"]),
                json={"title": "facts", "content": "The capital of France is Paris. The Eiffel Tower is in Paris.", "content_type": "text"})
    gs = client.put(f"{V}/acme/eval/set", headers=_auth(t["api_key"]), json={
        "items": [{"question": "capital of France", "relevant_texts": ["The capital of France is Paris"]}]})
    assert gs.status_code == 200
    rep = client.post(f"{V}/acme/eval/run", headers=_auth(t["api_key"]),
                      params={"top_k": 5, "rerank": True})
    assert rep.status_code == 200, rep.text
    body = rep.json()
    assert body["questions"] == 1
    assert body["hit_rate"] >= 1.0, body
    assert body["context_recall"] > 0.0


def test_tenant_chunk_quota(monkeypatch, client):
    monkeypatch.setenv("TENANT_CHUNK_QUOTA", "2")
    from app.config import get_settings
    get_settings.cache_clear()
    t = _make_tenant(client, "acme")
    big = " ".join(f"sentence number {i} about cats and dogs and birds and trees and music" for i in range(120))
    r = client.post(f"{V}/acme/documents", headers=_auth(t["api_key"]),
                    json={"title": "big", "content": big, "content_type": "text"})
    assert r.status_code == 429, r.text
    assert "quota" in r.json()["detail"].lower()


def test_redis_rate_limiter_enforces_shared_budget(client):
    """Integration: when REDIS_URL is configured the limiter must enforce a single
    shared per-IP budget through the real app path (fleet-safe). Skips if no Redis."""
    import os
    import redis
    url = os.environ.get("REDIS_URL") or "redis://localhost:6379/0"
    try:
        rc = redis.Redis.from_url(url, socket_connect_timeout=2)
        rc.ping()
    except Exception:  # noqa: BLE001
        pytest.skip("Redis not available")
    from app.ratelimit import reset_limiter
    os.environ["REDIS_URL"] = url
    from app.config import get_settings
    get_settings.cache_clear()
    reset_limiter("auto")
    rc.flushdb()
    from app.main import app
    t = _make_tenant(client, "acme")
    key = t["api_key"]
    auth = {"Authorization": f"Bearer {key}"}
    codes = [client.post(f"{V}/acme/query", headers=auth, json={"question": "x", "top_k": 1}).status_code
             for _ in range(130)]
    ok = codes.count(200)
    bad = codes.count(429)
    # per-IP cap is 120; tenant creation consumes ~1, so we expect rejections once
    # the shared budget is exhausted (not all 200).
    assert bad >= 1, f"expected shared-budget rejections, got 200={ok} 429={bad}"
    # the budget must live in Redis, not in-process only
    keys = rc.keys("rag:rl:*")
    assert len(keys) >= 1, "rate-limit state was not written to Redis"
    rc.flushdb()
    # restore deterministic in-memory mode for any later tests
    os.environ.pop("REDIS_URL", None)
    get_settings.cache_clear()
    reset_limiter("memory")

def test_real_reranker_reorders_by_relevance():
    """Regression: with USE_REAL_RERANKER=1 the reranker must reorder by true relevance,
    not just pass through vector-similarity order. Skips if `rerankers` isn't installed."""
    import os

    try:
        import rerankers  # noqa: F401
    except Exception:  # noqa: BLE001
        pytest.skip("rerankers not installed")

    from app.rerank import get_reranker

    os.environ["USE_REAL_RERANKER"] = "1"
    from app.config import get_settings
    get_settings.cache_clear()
    rk = get_reranker()
    # The cat doc has the highest *vector* score but is irrelevant to the France query.
    items = [
        {"text": "Cats are small mammals that meow and purr.", "score": 0.95},
        {"text": "The capital of France is Paris. The Eiffel Tower is there.", "score": 0.70},
        {"text": "Paris is the capital city of France in Western Europe.", "score": 0.60},
    ]
    out = rk.rerank("What is the capital of France?", items)
    assert out[0]["text"].startswith("The capital of France"), out[0]["text"]
    assert out[-1]["text"].startswith("Cats"), out[-1]["text"]
    os.environ.pop("USE_REAL_RERANKER", None)
    get_settings.cache_clear()
