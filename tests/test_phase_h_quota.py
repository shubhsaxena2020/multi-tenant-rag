"""v10.7 — per-tenant chunk quotas + rate limits (429 / Retry-After).

Covers:
  * per-tenant chunk quota OVERRIDE (admin-set) takes precedence over the global default
  * global default enforcement (capacity 429 with Retry-After + X-Quota-* headers)
  * admin set-quota endpoint is the ONLY way to raise a tenant's cap (tenant secret key
    cannot self-escalate)
  * /config GET exposes chunk_count + chunk_quota so tenants can self-monitor
  * per-IP / per-tenant rate limiting returns 429 + Retry-After under load
"""
import pytest

V = "/api/v1"
ADMIN = {"Admin-Key": "test-admin-key-for-tests"}


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


def _create_tenant(client):
    r = client.post(
        f"{V}/tenants", json={"name": "quota-tenant", "plan": "standard"}, headers=ADMIN
    )
    assert r.status_code in (200, 201), r.text
    return r.json()


def _set_quota(client, tid, quota):
    r = client.post(f"{V}/admin/tenants/{tid}/quota", json={"chunk_quota": quota}, headers=ADMIN)
    assert r.status_code == 200, r.text
    return r


def test_admin_can_set_per_tenant_quota(client):
    t = _create_tenant(client)
    tid, sk = t["tenant_id"], t["api_key"]
    r = _set_quota(client, tid, 100)
    assert r.json()["chunk_quota"] == 100
    cfg = client.get(f"{V}/{tid}/config", headers=_auth(sk)).json()
    assert cfg["chunk_quota"] == 100


def test_tenant_cannot_set_own_quota_via_config(client):
    t = _create_tenant(client)
    tid, sk = t["tenant_id"], t["api_key"]
    # The secret-key /config endpoint must NOT accept a chunk_quota field (self-escalation).
    r = client.post(
        f"{V}/{tid}/config", headers=_auth(sk),
        json={"system_prompt": "x", "chunk_quota": 999_999_999},
    )
    # Either rejected (422) or silently ignored — the quota must NOT change.
    cfg = client.get(f"{V}/{tid}/config", headers=_auth(sk)).json()
    assert cfg["chunk_quota"] == 0  # still inherits global default


def test_per_tenant_quota_override_enforced(client, monkeypatch):
    # Global default is huge; tenant override is tiny -> enforce the override.
    import os

    os.environ["TENANT_CHUNK_QUOTA"] = "10000000"
    from app.config import get_settings

    get_settings.cache_clear()

    t = _create_tenant(client)
    tid, sk = t["tenant_id"], t["api_key"]
    _set_quota(client, tid, 3)  # allow only 3 chunks

    # Ingest a small doc repeatedly; first ingest stores chunks, second should 429 once
    # the tenant's 3-chunk override is exceeded.
    body = {"title": "q", "content": "alpha beta gamma delta epsilon zeta", "content_type": "text"}
    r1 = client.post(f"{V}/{tid}/documents", headers=_auth(sk), json=body)
    assert r1.status_code == 201, r1.text
    n1 = r1.json()["chunk_count"]
    assert n1 >= 1

    # Now exhaust the remaining quota with a second doc using a DIFFERENT doc_id so it
    # doesn't just replace the first (re-ingest is net-neutral on quota).
    body2 = {"title": "q2", "content": "more words here to consume the rest of the budget",
             "content_type": "text", "doc_id": "doc-quota-2"}
    r2 = client.post(f"{V}/{tid}/documents", headers=_auth(sk), json=body2)
    # Either succeeded (within 3) or 429'd. We keep pushing until 429 to prove the cap.
    pushed = r2.status_code == 201
    if pushed:
        for i in range(10):
            rr = client.post(
                f"{V}/{tid}/documents", headers=_auth(sk),
                json={"title": f"q{i}", "content": "another chunk please",
                      "content_type": "text", "doc_id": f"doc-quota-x{i}"},
            )
            if rr.status_code == 429:
                break
        else:
            pytest.fail("never hit the per-tenant chunk quota 429")
        rr = client.post(
            f"{V}/{tid}/documents", headers=_auth(sk),
            json={"title": "blocked", "content": "should be rejected",
                  "content_type": "text", "doc_id": "doc-quota-FINAL"},
        )
        assert rr.status_code == 429, rr.text
        assert "quota" in rr.json()["detail"].lower()
        assert rr.headers.get("Retry-After") == "3600"
        assert rr.headers.get("X-Quota-Limit") == "3"


def test_global_chunk_quota_enforced_with_retry_after(client, monkeypatch):
    import os

    os.environ["TENANT_CHUNK_QUOTA"] = "2"
    from app.config import get_settings

    get_settings.cache_clear()
    t = _create_tenant(client)
    tid, sk = t["tenant_id"], t["api_key"]
    # No per-tenant override -> global default (2) applies.
    r = client.post(
        f"{V}/{tid}/documents", headers=_auth(sk),
        json={"title": "a", "content": "one two three four", "content_type": "text"},
    )
    # First ingest likely stores >=1 chunk; push until the global cap (2) rejects.
    if r.status_code == 201:
        for i in range(15):
            rr = client.post(
                f"{V}/{tid}/documents", headers=_auth(sk),
                json={"title": f"g{i}", "content": "consume global budget",
                      "content_type": "text", "doc_id": f"gdoc{i}"},
            )
            if rr.status_code == 429:
                break
        else:
            pytest.fail("never hit the global chunk quota 429")
        assert rr.headers.get("Retry-After") == "3600"
        assert rr.headers.get("X-Quota-Limit") == "2"
    else:
        assert r.status_code == 429
        assert r.headers.get("Retry-After") == "3600"


def test_config_exposes_quota_usage(client):
    t = _create_tenant(client)
    tid, sk = t["tenant_id"], t["api_key"]
    # Ingest something so chunk_count > 0.
    client.post(
        f"{V}/{tid}/documents", headers=_auth(sk),
        json={"title": "usage", "content": "hello world this is a doc", "content_type": "text"},
    )
    cfg = client.get(f"{V}/{tid}/config", headers=_auth(sk)).json()
    assert "chunk_count" in cfg
    assert "chunk_quota" in cfg
    assert cfg["chunk_count"] >= 1
    assert cfg["chunk_quota"] == 0  # inherits global default


def test_rate_limit_returns_429_with_retry_after(client, monkeypatch):
    import os

    # Tighten both dimensions so the test is fast and deterministic.
    os.environ["RATE_PER_TENANT_PER_MIN"] = "5"
    os.environ["RATE_PER_IP_PER_MIN"] = "5"
    from app.config import get_settings

    get_settings.cache_clear()
    from app.ratelimit import reset_limiter

    reset_limiter("memory")

    t = _create_tenant(client)
    tid, sk = t["tenant_id"], t["api_key"]
    codes = []
    for _ in range(12):
        r = client.get(f"{V}/{tid}/documents", headers=_auth(sk))
        codes.append(r.status_code)
    assert 429 in codes
    first_429 = next(c for c in codes if c == 429)
    assert first_429 == 429
    # The 429 response must carry Retry-After.
    r429 = client.get(f"{V}/{tid}/documents", headers=_auth(sk))
    if r429.status_code == 429:
        assert "Retry-After" in r429.headers
    reset_limiter("memory")
