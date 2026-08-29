"""PHASE B (#7-#11) — pre-retrieval query rewriting & decomposition.

Improves retrieval recall by clarifying ambiguous/short queries and decomposing multi-part
questions BEFORE the vector search runs. The module is a pure passthrough when no LLM is
configured (mirrors the repo's deterministic-default pattern: the RAG service must work with
zero paid credentials), and upgrades to an LLM-backed rewrite when `LLM_BASE_URL` is set.

The rewrite produces:
  - `rewritten_query`: a single self-contained query actually used for retrieval (for short
    queries this expands context; for multi-part questions it is the clarified whole).
  - `sub_questions`: when the question decomposes (e.g. "compare X and Y"), the individual
    sub-questions — surfaced for transparency/observability (and consumed by Phase C multi-hop).

It NEVER raises: any LLM failure falls back to the original question so the query path is
always available.
"""

from __future__ import annotations

import json
import re
from typing import Iterable

from app.config import get_settings


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.?!])\s+|\n+", text.strip())
    return [p.strip() for p in parts if p.strip()]


def _heuristic_decompose(question: str) -> list[str]:
    """Deterministic, no-LLM decomposition for obvious multi-part questions.

    Catches 'compare X and Y' / 'X vs Y' / 'difference between X and Y' shapes and returns the
    two sides as sub-questions. Returns [] when the question is single-part.
    """
    q = question.strip()
    m = re.search(r"\bcompare\s+(.+?)\s+(?:and|with|to)\s+(.+)$", q, re.IGNORECASE)
    if m:
        return [f"{m.group(1).strip()}?", f"{m.group(2).strip()}?"]
    m = re.search(r"\b(.+?)\s+vs\.?\s+(.+)$", q, re.IGNORECASE)
    if m and len(q) < 120:
        return [f"{m.group(1).strip()}?", f"{m.group(2).strip()}?"]
    m = re.search(r"\bdifference\s+between\s+(.+?)\s+and\s+(.+)$", q, re.IGNORECASE)
    if m:
        return [f"What is {m.group(1).strip()}?", f"What is {m.group(2).strip()}?"]
    return []


def _decompose_prompt(question: str) -> str:
    return (
        "You are a retrieval pre-processor for a document-search RAG system. Given a user "
        "question, produce a JSON object with two fields:\n"
        '  "rewritten_query": a single self-contained, unambiguous query that resolves references '
        "and adds minimal context so a vector search will retrieve the right passages;\n"
        '  "sub_questions": an array of independent sub-questions (usually 1, or 2-3 when the '
        "question is multi-part / comparative). Each sub-question must be answerable on its own.\n"
        "Output ONLY valid JSON, no prose.\n\n"
        f"Question: {question}"
    )


def _parse_llm_json(content: str) -> tuple[str, list[str]]:
    # Tolerate code-fenced JSON from some models.
    content = content.strip().strip("`").strip()
    if content.startswith("json"):
        content = content[4:].strip()
    data = json.loads(content)
    rewritten = str(data.get("rewritten_query") or "").strip()
    subs = [str(x).strip() for x in (data.get("sub_questions") or []) if str(x).strip()]
    return rewritten, subs


def rewrite_query(
    question: str,
    history: Iterable[str] | None = None,
    *,
    enabled: bool = True,
) -> tuple[str, list[str], bool]:
    """Pre-retrieval rewrite.

    Returns (rewritten_query, sub_questions, was_rewritten).

    - Passthrough (original question, [], False) when `enabled is False` or no LLM is configured.
    - LLM-backed rewrite (expansion + decomposition) when `LLM_BASE_URL` is set; falls back to the
      original question on any error. The deterministic heuristic decomposition is used as a
      fallback when the LLM is unavailable but the caller still wants decomposition hints.
    """
    if not enabled:
        return question, [], False
    s = get_settings()
    if not (s.llm_base_url and s.llm_api_key and s.llm_model):
        # No LLM: passthrough for retrieval, but still expose a deterministic decomposition hint.
        return question, _heuristic_decompose(question), False

    try:
        import httpx

        chat = []
        if history:
            joined = "\n".join(str(h) for h in history)
            chat.append({"role": "user", "content": "Prior context:\n" + joined})
        chat.append({"role": "user", "content": _decompose_prompt(question)})
        resp = httpx.post(
            s.llm_base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {s.llm_api_key}", "Content-Type": "application/json"},
            json={"model": s.llm_model, "messages": chat, "temperature": 0.0},
            timeout=20,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        rewritten, subs = _parse_llm_json(content)
        if not rewritten:
            rewritten = question
        return rewritten, subs or _heuristic_decompose(question), True
    except Exception:
        # Any LLM failure must not break the query path.
        return question, _heuristic_decompose(question), False
