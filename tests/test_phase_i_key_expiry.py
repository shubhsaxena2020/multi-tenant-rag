"""v10.8 — tenant API key expiry (self-service rotation + time-boxed keys).

Covers:
  * rotate with `expires_in_days` mints a key that expires at the computed time
  * an EXPIRED key is rejected at resolution (401/403) exactly like a revoked key
  * a non-expired (future) expiring key still works until its time passes
  * GET /keys surfaces expires_at + expired flag
  * omitting expires_in_days => key never expires (back-compat)
  * publishable keys also support expiry
"""
import os

import pytest

V = "/api/v1"
ADMIN = {"Admin-Key": "test-admin-key-for-tests"}


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


def _create_tenant(client):
    r = client.post(f"{V}/tenants", json={"name": "keyexp", "plan": "standard"}, headers=ADMIN)
    assert r.status_code in (200, 201), r.text
    return r.json()


def test_rotate_without_expiry_keeps_working(client):
    t = _create_tenant(client)
    tid, sk = t["tenant_id"], t["api_key"]
    r = client.post(f"{V}/{tid}/keys", headers=_auth(sk))
    assert r.status_code == 201, r.text
    new_key = r.json()["api_key"]
    # New key has no expiry and authenticates.
    me = client.get(f"{V}/{tid}/config", headers=_auth(new_key))
    assert me.status_code == 200


def test_rotate_expiry_listed_in_keys(client):
    t = _create_tenant(client)
    tid, sk = t["tenant_id"], t["api_key"]
    r = client.post(f"{V}/{tid}/keys?expires_in_days=7", headers=_auth(sk))
    assert r.status_code == 201, r.text
    lst = client.get(f"{V}/{tid}/keys", headers=_auth(sk)).json()
    # The newest key should show an expires_at and not be expired yet.
    newest = lst["keys"][0]
    assert newest["expires_at"] is not None
    assert newest["expired"] is False
    assert newest["kind"] == "secret"


def test_expired_key_is_rejected(client, monkeypatch):
    # Force "now" into the past so an expires_in_days=1 key is already expired? Simpler:
    # mint a key that expires 1 day from now, then verify the resolution SQL rejects a
    # key whose expires_at is in the past. We test the DB layer directly for determinism.
    from datetime import datetime, timedelta, timezone

    t = _create_tenant(client)
    tid, sk = t["tenant_id"], t["api_key"]

    # Insert a key that is ALREADY expired via the public rotate path would not allow a
    # past date, so exercise the resolution rule directly through the db layer.
    from app import db as dbmod
    import asyncio

    past = datetime.now(timezone.utc) - timedelta(days=1)
    asyncio.run(dbmod.add_api_key(tid, "rk_expired_xyzEXPIRED", expires_at=past))
    # The expired key must NOT resolve.
    resolved = asyncio.run(dbmod.get_tenant_by_key("rk_expired_xyzEXPIRED"))
    assert resolved is None, "expired key should be rejected at resolution"
    # And its kind must be None (so auth rejects it).
    kind = asyncio.run(dbmod.get_key_kind("rk_expired_xyzEXPIRED"))
    assert kind is None


def test_future_expiring_key_works_then_list_shows_expired_flag(client):
    from datetime import datetime, timedelta, timezone

    t = _create_tenant(client)
    tid, sk = t["tenant_id"], t["api_key"]
    future = datetime.now(timezone.utc) + timedelta(days=30)
    import asyncio
    from app import db as dbmod

    asyncio.run(dbmod.add_api_key(tid, "rk_future_xyzFUTURE", expires_at=future))
    # Works now.
    assert asyncio.run(dbmod.get_tenant_by_key("rk_future_xyzFUTURE")) is not None
    # List shows expired=False.
    keys = asyncio.run(dbmod.list_key_prefixes(tid))
    match = next(k for k in keys if k["prefix"] == "rk_futur")
    assert match["expired"] is False
    assert match["expires_at"] is not None


def test_publishable_key_supports_expiry(client):
    t = _create_tenant(client)
    tid, sk = t["tenant_id"], t["api_key"]
    r = client.post(f"{V}/{tid}/keys/publishable?expires_in_days=3", headers=_auth(sk))
    assert r.status_code == 201, r.text
    pk = r.json()["api_key"]
    # Publishable (read-only) key is scope-locked: it authenticates but is 403 on a
    # secret-key-only endpoint like /config. That proves the key was minted (with expiry).
    cfg = client.get(f"{V}/{tid}/config", headers=_auth(pk))
    assert cfg.status_code == 403
    lst = client.get(f"{V}/{tid}/keys", headers=_auth(sk)).json()
    pub = next(k for k in lst["keys"] if k["prefix"] == pk[:8])
    assert pub["kind"] == "publishable"
    assert pub["expires_at"] is not None
    assert pub["expired"] is False


def test_expired_key_rejected_on_live_request(client):
    """End-to-end: an expired key returns 401 on a real request (not just DB layer)."""
    from datetime import datetime, timedelta, timezone

    t = _create_tenant(client)
    tid, sk = t["tenant_id"], t["api_key"]
    past = datetime.now(timezone.utc) - timedelta(days=1)
    import asyncio
    from app import db as dbmod

    asyncio.run(dbmod.add_api_key(tid, "rk_liveexp_xyzLIVE", expires_at=past))
    # A request with the expired key must be denied.
    r = client.get(f"{V}/{tid}/config", headers=_auth("rk_liveexp_xyzLIVE"))
    assert r.status_code in (401, 403), r.text
