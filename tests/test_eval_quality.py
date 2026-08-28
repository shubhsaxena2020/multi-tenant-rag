"""v9-4: continuous eval & answer-quality monitoring (self-hosted LLM judge + trend tracking)."""
import os

import pytest

from app.eval import EvalItem, JudgeLLM, evaluate_quality, save_eval_run, load_eval_runs

V = "/api/v1"


def test_judge_lexical_fallback():
    """v9-4: when no judge endpoint is configured, metrics fall back to lexical heuristics
    (deterministic, flagged unavailable) rather than crashing."""
    j = JudgeLLM(base_url="", api_key="", model="")  # no judge configured
    assert j.available is False
    # grounded answer -> high faithfulness
    f1 = j.faithfulness("q", "The cat sat on the mat.", "The cat sat on the mat.")
    assert f1 > 0.5
    # hallucinated answer -> low faithfulness
    f2 = j.faithfulness("q", "The cat sat on the mat.", "The dog flew to Mars.")
    assert f2 < f1
    # relevant vs irrelevant answer
    assert j.answer_relevancy("What color is the sky?", "The sky is blue.") > \
           j.answer_relevancy("What color is the sky?", "Bananas are yellow fruit.")


def test_quality_eval_persists_trend(client):
    """v9-4: /eval/quality computes metrics AND persists a run to the trend history."""
    r = client.post(f"{V}/tenants", json={"name": "eq"}, headers={"Admin-Key": os.environ.get("ADMIN_API_KEY")})
    key = r.json()["api_key"]
    auth = {"Authorization": f"Bearer {key}"}
    # ingest a doc + golden set
    client.post(f"{V}/eq/documents", headers=auth,
                json={"title": "capitals", "content": "The capital of France is Paris. It is in Europe.", "content_type": "text"})
    client.put(f"{V}/eq/eval/set", headers=auth, json={
        "items": [{"question": "capital of France?", "relevant_texts": ["The capital of France is Paris."]}]})

    resp = client.post(f"{V}/eq/eval/quality?persist=true", headers=auth)
    assert resp.status_code == 200
    body = resp.json()
    assert "hit_rate" in body and "faithfulness" in body and "answer_relevancy" in body
    assert "run_id" in body

    runs = client.get(f"{V}/eq/eval/runs", headers=auth).json()
    assert len(runs) >= 1
    assert runs[-1]["hit_rate"] == body["hit_rate"]


def test_auto_golden_requires_judge(client, monkeypatch):
    """v9-4: golden auto-gen returns [] (clearly) when no judge model is configured."""
    r = client.post(f"{V}/tenants", json={"name": "ag"}, headers={"Admin-Key": os.environ.get("ADMIN_API_KEY")})
    key = r.json()["api_key"]
    auth = {"Authorization": f"Bearer {key}"}
    resp = client.post(f"{V}/ag/eval/golden/auto", headers=auth,
                       json={"title": "doc", "text": "Paris is the capital of France."})
    assert resp.status_code == 200
    assert resp.json()["generated"] == []
    assert resp.json()["judge_available"] is False


def test_load_eval_runs_empty(client):
    r = client.post(f"{V}/tenants", json={"name": "er"}, headers={"Admin-Key": os.environ.get("ADMIN_API_KEY")})
    key = r.json()["api_key"]
    auth = {"Authorization": f"Bearer {key}"}
    assert client.get(f"{V}/er/eval/runs", headers=auth).json() == []
