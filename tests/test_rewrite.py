"""PHASE B (#7-#11) — pre-retrieval query rewriting & decomposition.

- rewrite_query() is a pure passthrough when no LLM is configured (zero paid credentials ok).
- rewrite_query() expands/clarifies short queries and decomposes multi-part questions when an
  LLM is configured (mocked here so the test needs no network/key).
- The /query route honors `rewrite=false` (raw question) and surfaces `rewritten_query` /
  `sub_questions` when rewriting applies.
"""

import json

import pytest

from app.retrieval.rewrite import rewrite_query, _heuristic_decompose


def _set_llm(monkeypatch, on: bool):
    if on:
        monkeypatch.setenv("LLM_BASE_URL", "http://llm")
        monkeypatch.setenv("LLM_API_KEY", "k")
        monkeypatch.setenv("LLM_MODEL", "m")
    else:
        monkeypatch.setenv("LLM_BASE_URL", "")
        monkeypatch.setenv("LLM_API_KEY", "")
        monkeypatch.setenv("LLM_MODEL", "")
    # get_settings() is lru_cached; invalidate so the new env is read.
    from app.config import get_settings
    get_settings.cache_clear()


# ---------------- unit: passthrough (no LLM configured) ----------------
def test_passthrough_without_llm(monkeypatch):
    _set_llm(monkeypatch, False)
    q = "What is RAG?"
    out, subs, used = rewrite_query(q, enabled=True)
    assert out == q
    assert used is False
    assert isinstance(subs, list)


def test_disabled_is_pure_passthrough(monkeypatch):
    _set_llm(monkeypatch, True)
    q = "compare cats and dogs"
    out, subs, used = rewrite_query(q, enabled=False)
    assert out == q
    assert used is False


def test_heuristic_decompose_multi_part():
    assert len(_heuristic_decompose("Compare cats and dogs")) == 2
    assert len(_heuristic_decompose("React vs Vue")) == 2
    assert len(_heuristic_decompose("What is retrieval?")) == 0


# ---------------- unit: LLM-backed rewrite (mocked httpx) ----------------
class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _fake_post_single():
    def _post(url, **kwargs):
        return _FakeResp({"choices": [{"message": {"content": json.dumps({
            "rewritten_query": "What is retrieval-augmented generation and how does it work?",
            "sub_questions": ["What is retrieval-augmented generation and how does it work?"],
        })}}]})
    return _post


def _fake_post_compare():
    def _post(url, **kwargs):
        return _FakeResp({"choices": [{"message": {"content": json.dumps({
            "rewritten_query": "Compare cats and dogs across temperament and care.",
            "sub_questions": ["What is the temperament of cats?", "What is the temperament of dogs?"],
        })}}]})
    return _post


def test_llm_rewrite_single_query(monkeypatch):
    _set_llm(monkeypatch, True)
    import httpx
    monkeypatch.setattr(httpx, "post", _fake_post_single())
    out, subs, used = rewrite_query("what is rag", enabled=True)
    assert used is True
    assert "retrieval-augmented generation" in out
    assert len(subs) == 1


def test_llm_rewrite_compare_query(monkeypatch):
    _set_llm(monkeypatch, True)
    import httpx
    monkeypatch.setattr(httpx, "post", _fake_post_compare())
    out, subs, used = rewrite_query("compare cats and dogs", enabled=True)
    assert used is True
    assert len(subs) == 2


def test_llm_rewrite_falls_back_on_error(monkeypatch):
    _set_llm(monkeypatch, True)

    def _boom(*a, **k):
        raise RuntimeError("llm down")
    import httpx
    monkeypatch.setattr(httpx, "post", _boom)
    q = "compare cats and dogs"
    out, subs, used = rewrite_query(q, enabled=True)
    assert out == q
    assert used is False
    assert len(subs) == 2


# ---------------- integration: /query honors rewrite flag ----------------
def _make_tenant(client, name):
    r = client.post("/api/v1/tenants", headers={"Admin-Key": "test-admin-key-for-tests"},
                    json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


def test_query_rewrite_flag_and_passthrough(client, monkeypatch):
    _set_llm(monkeypatch, False)
    t = _make_tenant(client, "rewritetest")
    doc = client.post(
        f"/api/v1/{t['tenant_id']}/documents",
        headers=_auth(t["api_key"]),
        json={"title": "doc", "content": "RAG stands for retrieval-augmented generation.", "content_type": "text"},
    )
    assert doc.status_code == 201, doc.text

    q = client.post(
        f"/api/v1/{t['tenant_id']}/query", headers=_auth(t["api_key"]),
        json={"question": "What is RAG?", "top_k": 3},
    )
    assert q.status_code == 200, q.text
    body = q.json()
    assert body["rewritten_query"] is None
    assert body["sub_questions"] is None

    q2 = client.post(
        f"/api/v1/{t['tenant_id']}/query", headers=_auth(t["api_key"]),
        json={"question": "What is RAG?", "top_k": 3, "rewrite": False},
    )
    assert q2.status_code == 200
    assert q2.json()["rewritten_query"] is None
    assert "retrieval-augmented" in " ".join(h["text"] for h in q2.json()["results"])
