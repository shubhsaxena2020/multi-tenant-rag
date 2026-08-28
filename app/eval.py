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
    return {w for w in t.lower().split() if len(w) > 2}


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
from .db import get_session_maker
from sqlalchemy import text
import json
from dataclasses import asdict


async def save_golden_set(tenant_id: str, items: list[EvalItem]) -> int:
    """Save golden set for a tenant using the same DB layer as tenants."""
    session_maker = get_session_maker()
    async with session_maker() as session:
        # Create table if not exists (using raw SQL for simplicity)
        await session.execute(text("""
            CREATE TABLE IF NOT EXISTS eval_sets (
                tenant_id TEXT NOT NULL, 
                question TEXT, 
                relevant_doc_ids TEXT, 
                relevant_texts TEXT, 
                expected_answer TEXT
            )
        """))
        # Delete existing entries for this tenant
        await session.execute(text("DELETE FROM eval_sets WHERE tenant_id = :tenant_id"), 
                           {"tenant_id": tenant_id})
        # Insert new items
        for it in items:
            await session.execute(text("""
                INSERT INTO eval_sets VALUES (:tenant_id, :question, :relevant_doc_ids, :relevant_texts, :expected_answer)
            """), {
                "tenant_id": tenant_id,
                "question": it.question,
                "relevant_doc_ids": json.dumps(it.relevant_doc_ids),
                "relevant_texts": json.dumps(it.relevant_texts),
                "expected_answer": it.expected_answer
            })
        await session.commit()
    return len(items)


async def load_golden_set(tenant_id: str) -> list[EvalItem]:
    """Load golden set for a tenant using the same DB layer as tenants."""
    session_maker = get_session_maker()
    async with session_maker() as session:
        result = await session.execute(text("""
            SELECT question, relevant_doc_ids, relevant_texts, expected_answer 
            FROM eval_sets WHERE tenant_id = :tenant_id
        """), {"tenant_id": tenant_id})
        rows = result.fetchall()
        return [
            EvalItem(
                question=r[0],
                relevant_doc_ids=json.loads(r[1] or "[]"),
                relevant_texts=json.loads(r[2] or "[]"),
                expected_answer=r[3] or "",
            )
            for r in rows
        ]


# ---------------- v9-4: LLM-as-judge answer-quality + trend tracking ----------------

class JudgeLLM:
    """Self-hosted LLM-as-judge (2026 best practice: keep eval judge on your own VPS node,
    not an external paid API — avoids cost + data-leakage). Uses the same OpenAI-compatible
    `llm_base_url` the service already has, so no extra dependency. If no judge endpoint is
    configured, metrics fall back to deterministic lexical heuristics (clearly flagged)."""

    def __init__(self, base_url: str | None = None, api_key: str = "", model: str = ""):
        from .config import get_settings

        s = get_settings()
        self.base_url = base_url or s.llm_base_url
        self.api_key = api_key or s.llm_api_key
        self.model = model or s.llm_model
        self.available = bool(self.base_url and self.api_key and self.model)

    def _chat(self, system: str, user: str) -> str:
        import httpx

        resp = httpx.post(
            self.base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json={"model": self.model, "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ], "temperature": 0.0},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()

    def faithfulness(self, question: str, context: str, answer: str) -> float:
        """1.0 if the answer is fully supported by the context, 0.0 if it contradicts/
        hallucinates. Self-hosted judge returns 0-1; lexical fallback uses token overlap."""
        if not self.available:
            return self._faithfulness_lexical(context, answer)
        try:
            out = self._chat(
                "You are a strict faithfulness grader. Reply with only a number from 0.0 to 1.0 "
                "indicating how well the ANSWER is supported by the CONTEXT (1.0 = fully grounded, "
                "0.0 = contradicts or adds ungrounded facts). No explanation.",
                f"QUESTION: {question}\n\nCONTEXT:\n{context[:4000]}\n\nANSWER:\n{answer[:1500]}",
            )
            return _clamp_float(out)
        except Exception:
            return self._faithfulness_lexical(context, answer)

    def answer_relevancy(self, question: str, answer: str) -> float:
        if not self.available:
            return self._relevancy_lexical(question, answer)
        try:
            out = self._chat(
                "You are a relevancy grader. Reply with only a number 0.0-1.0 for how directly the "
                "ANSWER addresses the QUESTION (1.0 = perfectly on-topic, 0.0 = irrelevant). No explanation.",
                f"QUESTION: {question}\n\nANSWER:\n{answer[:1500]}",
            )
            return _clamp_float(out)
        except Exception:
            return self._relevancy_lexical(question, answer)

    def _faithfulness_lexical(self, context: str, answer: str) -> float:
        c, a = _norm(context), _norm(answer)
        if not a:
            return 0.0
        return min(1.0, len(c & a) / len(a))

    def _relevancy_lexical(self, question: str, answer: str) -> float:
        q, a = _norm(question), _norm(answer)
        if not a or not q:
            return 0.0
        return min(1.0, len(q & a) / len(q))

    def generate_golden(self, doc_title: str, doc_text: str, n: int = 3) -> list[EvalItem]:
        """Auto-generate golden QA pairs from a document using the judge (v9-4 auto-golden)."""
        if not self.available:
            return []  # cannot auto-generate without a judge model
        try:
            out = self._chat(
                "You generate evaluation questions for a RAG system. Given a document, produce "
                f"{n} question/answer pairs whose answers are fully grounded in the document. "
                "Reply as JSON: [{\"question\": ..., \"expected_answer\": ...}, ...]. No prose.",
                f"TITLE: {doc_title}\n\nDOCUMENT:\n{doc_text[:4000]}",
            )
            data = json.loads(_extract_json(out))
            return [EvalItem(question=d["question"], relevant_texts=[doc_text],
                             expected_answer=d.get("expected_answer", "")) for d in data]
        except Exception:
            return []


def _clamp_float(s: str) -> float:
    import re

    m = re.search(r"(\d+(?:\.\d+)?)", s)
    if not m:
        return 0.0
    v = float(m.group(1))
    return max(0.0, min(1.0, v))


def _extract_json(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1]
        if s.startswith("json"):
            s = s[4:]
    a, b = s.find("["), s.rfind("]")
    if a != -1 and b != -1:
        return s[a:b + 1]
    return s


@dataclass
class QualityReport(EvalReport):
    faithfulness: float = 0.0
    answer_relevancy: float = 0.0
    judge_available: bool = False


def evaluate_quality(
    tenant_id: str,
    items: list[EvalItem],
    top_k: int = 8,
    candidate_k: int = 100,
    rerank: bool = True,
    generate_answer: bool = True,
) -> QualityReport:
    """Extended eval that ALSO scores answer faithfulness + relevancy via the self-hosted
    judge (v9-4). When no judge is configured, falls back to lexical heuristics and
    judge_available=False."""
    import time

    from .generation import generate_answer as _gen

    judge = JudgeLLM()
    base = evaluate(tenant_id, items, top_k=top_k, candidate_k=candidate_k, rerank=rerank)
    faith, rel = 0.0, 0.0
    n = max(1, len(items))
    for it in items:
        results = retrieve(tenant_id, it.question, top_k=top_k, candidate_k=candidate_k, rerank=rerank)
        ctx = "\n".join(h["text"] for h in results)
        answer = ""
        if generate_answer and results:
            answer = _gen(it.question, results)
        faith += judge.faithfulness(it.question, ctx, answer) if answer else 0.0
        rel += judge.answer_relevancy(it.question, answer) if answer else 0.0
    return QualityReport(
        questions=base.questions, hit_rate=base.hit_rate, mrr=base.mrr, ndcg=base.ndcg,
        context_recall=base.context_recall, avg_latency_ms=base.avg_latency_ms,
        faithfulness=faith / n, answer_relevancy=rel / n, judge_available=judge.available,
    )


# ---- eval run history (trend tracking) ----

async def save_eval_run(tenant_id: str, report: QualityReport, run_kind: str = "manual") -> int:
    """Persist an eval run for trend tracking (v9-4). Returns the run id."""
    session_maker = get_session_maker()
    async with session_maker() as session:
        await session.execute(text("""
            CREATE TABLE IF NOT EXISTS eval_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT NOT NULL,
                run_kind TEXT,
                run_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                hit_rate REAL, mrr REAL, ndcg REAL, context_recall REAL,
                faithfulness REAL, answer_relevancy REAL, avg_latency_ms REAL
            )
        """))
        res = await session.execute(text("""
            INSERT INTO eval_runs (tenant_id, run_kind, hit_rate, mrr, ndcg, context_recall,
                                  faithfulness, answer_relevancy, avg_latency_ms)
            VALUES (:tid, :kind, :hr, :mrr, :ndcg, :cr, :faith, :rel, :lat)
            RETURNING id
        """), {
            "tid": tenant_id, "kind": run_kind,
            "hr": report.hit_rate, "mrr": report.mrr, "ndcg": report.ndcg,
            "cr": report.context_recall, "faith": report.faithfulness,
            "rel": report.answer_relevancy, "lat": report.avg_latency_ms,
        })
        rid = res.fetchone()
        await session.commit()
        return rid[0] if rid else -1


async def load_eval_runs(tenant_id: str, limit: int = 50) -> list[dict]:
    """Load recent eval runs (oldest last) for trend dashboards (v9-4)."""
    session_maker = get_session_maker()
    async with session_maker() as session:
        await session.execute(text("""
            CREATE TABLE IF NOT EXISTS eval_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT NOT NULL,
                run_kind TEXT,
                run_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                hit_rate REAL, mrr REAL, ndcg REAL, context_recall REAL,
                faithfulness REAL, answer_relevancy REAL, avg_latency_ms REAL
            )
        """))
        result = await session.execute(text("""
            SELECT id, run_at, hit_rate, mrr, ndcg, context_recall, faithfulness,
                   answer_relevancy, avg_latency_ms
            FROM eval_runs WHERE tenant_id = :tid ORDER BY id DESC LIMIT :lim
        """), {"tid": tenant_id, "lim": limit})
        rows = result.fetchall()
        return [dict(zip(
            ["id", "run_at", "hit_rate", "mrr", "ndcg", "context_recall",
             "faithfulness", "answer_relevancy", "avg_latency_ms"], r)) for r in rows]

