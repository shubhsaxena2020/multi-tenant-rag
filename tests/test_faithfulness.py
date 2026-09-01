"""PHASE D (#19-#24) — citation faithfulness & no-answer detection.

- score_faithfulness() is deterministic without an LLM: a grounded answer scores high, an
  ungrounded one low, and an empty/refusal answer is not answerable.
- /query (generate=true) surfaces `faithfulness` (0..1) + `answerable`; an unanswerable/out-of-scope
  query returns a safe "I don't know" answer with answerable=False and the citations still attached.
"""

import pytest

from app.faithfulness import (
    score_faithfulness,
    token_overlap,
    is_refusal,
)


# ---------------- unit: deterministic scoring ----------------
def test_token_overlap_grounded():
    ctx = "The Phoenix project launched in Q1 and moved the billing service to the cloud."
    ans = "The Phoenix project launched in Q1."
    score = token_overlap(ans, ctx)
    assert score > 0.5


def test_token_overlap_ungrounded():
    ctx = "The Phoenix project launched in Q1 and moved the billing service to the cloud."
    ans = "The Mars colony was established in 2099 by the Galactic Federation."
    assert token_overlap(ans, ctx) == 0.0


def test_is_refusal_variants():
    assert is_refusal("I don't know the answer.")
    assert is_refusal("I don't have information on that in the available documents.")
    assert is_refusal(None)
    assert is_refusal("") is True
    assert is_refusal("The Phoenix project launched in Q1.") is False


def test_score_faithfulness_grounded():
    ctx = ["The Phoenix project launched in Q1 and moved the billing service to the cloud."]
    score, answerable = score_faithfulness("The Phoenix project launched in Q1.", ctx)
    assert answerable is True
    assert score > 0.5


def test_score_faithfulness_ungrounded_answerable_flag():
    ctx = ["The Phoenix project launched in Q1 and moved the billing service to the cloud."]
    score, answerable = score_faithfulness("The Mars colony was established in 2099.", ctx)
    assert answerable is True  # has an answer, just poorly grounded
    assert score < 0.3


def test_score_faithfulness_refusal_not_answerable():
    score, answerable = score_faithfulness("I don't know.", ["Some context about cats."])
    assert answerable is False
    assert score == 0.0


# ---------------- integration: /query emits faithfulness + no-answer path ----------------
def _make_tenant(client, name):
    r = client.post("/api/v1/tenants", headers={"Admin-Key": "test-admin-key-for-tests"},
                    json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


def test_query_faithfulness_surface(client, monkeypatch):
    # No LLM configured -> extractive (grounded) answer; faithfulness high, answerable True.
    monkeypatch.setenv("LLM_BASE_URL", "")
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("LLM_MODEL", "")
    from app.config import get_settings
    get_settings.cache_clear()

    t = _make_tenant(client, "faithtest")
    doc = client.post(
        f"/api/v1/{t['tenant_id']}/documents", headers=_auth(t["api_key"]),
        json={"title": "doc", "content": "The Phoenix project launched in Q1 with the billing service.", "content_type": "text"},
    )
    assert doc.status_code == 201, doc.text
    q = client.post(
        f"/api/v1/{t['tenant_id']}/query", headers=_auth(t["api_key"]),
        json={"question": "When did the Phoenix project launch?", "top_k": 3, "generate": True},
    )
    assert q.status_code == 200, q.text
    body = q.json()
    assert body["faithfulness"] is not None
    assert 0.0 <= body["faithfulness"] <= 1.0
    assert body["answerable"] is True


def test_query_no_answer_path(client, monkeypatch):
    # No LLM -> a query with NO retrieved context (empty tenant) yields a graceful refusal with
    # answerable=False while citations (none here) would still be attached if present.
    monkeypatch.setenv("LLM_BASE_URL", "")
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("LLM_MODEL", "")
    from app.config import get_settings
    get_settings.cache_clear()

    t = _make_tenant(client, "noanswertest")
    # Intentionally ingest nothing; retrieval returns no context -> out of scope -> safe refusal.
    q = client.post(
        f"/api/v1/{t['tenant_id']}/query", headers=_auth(t["api_key"]),
        json={"question": "What is the meaning of life according to ancient philosophers?", "top_k": 3, "generate": True},
    )
    assert q.status_code == 200, q.text
    body = q.json()
    assert body["answerable"] is False
    assert "don't have" in (body["answer"] or "").lower() or "enough information" in (body["answer"] or "").lower()

# ---------------- adversarial: stopword preservation for negation ----------------
def test_stopword_preservation_for_negation():
    """Ensure 'no' and 'not' are preserved in _tokens for faithfulness/negation detection."""
    from app.faithfulness import _tokens

    # 'no' must NOT be stripped (critical for negation detection in faithfulness)
    assert 'no' in _tokens("no cats")
    assert 'cat' in _tokens("no cat")

    # 'not' must NOT be stripped (critical for negation detection)
    assert 'not' in _tokens("not relevant")

    # Normal stopwords should still be stripped
    assert 'the' not in _tokens("the cat")
    assert 'a' not in _tokens("a cat")
