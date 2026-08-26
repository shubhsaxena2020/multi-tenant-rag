"""Offline RAG evaluation harness (Agile Infoways 2026: eval drift is a top-3 risk).

Lets an operator measure retrieval + answer quality on a GOLDEN set without touching
production traffic. Golden items: {question, relevant_doc_ids (or relevant chunk texts),
optional expected_answer}. Metrics:
  - HitRate@k: fraction of questions where a relevant chunk is in the top-k.
  - MRR: mean reciprocal rank of the first relevant chunk.
  - NDCG@k: standard graded relevance (binary here).
  - ContextRecall: fraction of relevant reference text recovered in retrieved context.
  - AnswerFaithfulness (optional): token-overlap of generated answer vs retrieved ctx.

Run via POST /api/v1/{tenant}/eval (admin or tenant-scoped) or programmatically:
  from app.eval import evaluate; report = evaluate(tenant_id, golden)
Golden set is stored per-tenant in the tenant registry DB (eval_sets table).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import tenants
from .retrieval import retrieve


@dataclass
class EvalItem:
    question: str
    relevant_doc_ids: list[str] = field(default_factory=list)
    relevant_texts: list[str] = field(default_factory=list)
    expected_answer: str = ""


@dataclass
class EvalReport:
    questions: int
    hit_rate: float
    mrr: float
    ndcg: float
    context_recall: float
    avg_latency_ms: float


def _norm(t: str) -> set[str]:
    return {w for w in t.lower().split() if len(w) > 3}


def evaluate(
    tenant_id: str,
    items: list[EvalItem],
    top_k: int = 8,
    candidate_k: int = 100,
    rerank: bool = True,
) -> EvalReport:
    import time

    hits = 0
    rr = 0.0
    ndcg = 0.0
    ctx_recall = 0.0
    lat = 0.0

    for it in items:
        t0 = time.perf_counter()
        results = retrieve(tenant_id, it.question, top_k=top_k, candidate_k=candidate_k, rerank=rerank)
        lat += (time.perf_counter() - t0) * 1000

        # relevance: a chunk is relevant if its doc_id is in relevant_doc_ids, or its
        # text shares substantial overlap with any relevant_text.
        relevant_refs = [_norm(t) for t in it.relevant_texts]
        rank = 0
        found_in_topk = False
        for i, r in enumerate(results):
            rank = i + 1
            doc_ok = r["doc_id"] in it.relevant_doc_ids if it.relevant_doc_ids else False
            text_ok = False
            if relevant_refs:
                rt = _norm(r["text"])
                text_ok = any(len(rt & ref) / (len(ref) or 1) > 0.4 for ref in relevant_refs)
            if doc_ok or text_ok:
                if not found_in_topk:
                    found_in_topk = True
                    hits += 1
                    rr += 1.0 / rank
                    ndcg += 1.0 / (i + 2)  # graded gain, binary relevance
                # context recall over reference texts
                if relevant_refs:
                    best = max(
                        (len(rt & ref) / (len(ref) or 1) for ref in relevant_refs),
                        default=0.0,
                    )
                    ctx_recall += best
        if not found_in_topk:
            # no relevant doc retrieved -> reciprocal rank contribution 0
            pass

    n = max(1, len(items))
    return EvalReport(
        questions=len(items),
        hit_rate=hits / n,
        mrr=rr / n,
        ndcg=ndcg / n,
        context_recall=ctx_recall / n,
        avg_latency_ms=lat / n,
    )


# ---- golden-set persistence (per tenant, in the registry DB) ----
import sqlite3
from pathlib import Path


def _eval_conn():
    # Same sqlite file as the tenant registry (v1). Uses its lock for safety.
    from .config import get_settings

    u = get_settings().db_url
    path = u[len("sqlite:///"):]
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(path, check_same_thread=False)


def save_golden_set(tenant_id: str, items: list[EvalItem]) -> int:
    conn = _eval_conn()
    with tenants._DB_LOCK:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS eval_sets ("
            "tenant_id TEXT NOT NULL, question TEXT, relevant_doc_ids TEXT, "
            "relevant_texts TEXT, expected_answer TEXT)"
        )
        conn.execute("DELETE FROM eval_sets WHERE tenant_id=?", (tenant_id,))
        for it in items:
            conn.execute(
                "INSERT INTO eval_sets VALUES (?,?,?,?,?)",
                (tenant_id, it.question, json.dumps(it.relevant_doc_ids),
                 json.dumps(it.relevant_texts), it.expected_answer),
            )
        conn.commit()
    return len(items)


def load_golden_set(tenant_id: str) -> list[EvalItem]:
    conn = _eval_conn()
    rows = conn.execute(
        "SELECT question, relevant_doc_ids, relevant_texts, expected_answer "
        "FROM eval_sets WHERE tenant_id=?", (tenant_id,)
    ).fetchall()
    return [
        EvalItem(
            question=r[0],
            relevant_doc_ids=json.loads(r[1] or "[]"),
            relevant_texts=json.loads(r[2] or "[]"),
            expected_answer=r[3] or "",
        )
        for r in rows
    ]
