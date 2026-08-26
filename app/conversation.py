"""Conversation-aware RAG: query rewriting, intent/injection guarding, confidence gating.

Three production behaviors a single-turn search engine lacks (and a live website chat
widget needs), grounded in 2026 practice:

1. CONVERSATIONAL REWRITE (Alhena 2024; Microsoft Copilot Studio RAG guidance; StackOverflow
   multi-turn RAG): follow-up questions with pronouns / implicit references ("how much does
   it cost?" after a turn about the Pro plan) silently fail retrieval because the vector is
   computed from the bare phrase. We rewrite the current turn into a self-contained query
   using the recent session history BEFORE embedding. When an LLM is configured we ask it to
   rewrite; otherwise we fall back to a deterministic context-stuffing heuristic so the
   feature works with zero external dependencies.

2. INJECTION / OFF-TOPIC GUARD (PromptGuard / Rebuff pattern: guardrail as a preprocessing
   sidecar before the prompt reaches the model): detect overt instruction-injection and
   jailbreak patterns in the user turn. Flagged turns are still retrieved for benign lookup,
   but generation is gated (we do not forward a manipulative instruction into the LLM prompt
   unguarded, and we surface injection_detected in the response).

3. CONFIDENCE GATING / ABSTENTION (Self-RAG "know when not to answer"; hallucination
   mitigation): if the best retrieved chunk's score is below a calibrated threshold (or no
   chunks are returned), the question is treated as out-of-scope and we return a graceful
   "I don't have information on that" handoff instead of a hallucinated/extractive guess.

Sessions are keyed by session_id; history is kept server-side (per replica; for multi-replica
   fleets back this with Redis — see DEPLOYMENT.md). TTL/eviction keeps memory bounded.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field

from .config import get_settings

_MAX_TURNS = 12  # keep last N turns per session

# Overt injection / jailbreak surface forms (heuristic layer; PromptGuard-style).
_INJECTION_PATTERNS = [
    r"ignore (all |any |the )?(previous|prior|above|earlier) (instructions|prompt|messages|context)",
    r"disregard (the )?(previous|above|system|prior)",
    r"you are (now )?(a|an) .{0,40}(assistant|bot|model|ai|character)",
    r"(system|developer|admin) (prompt|instruction|message)",
    r"(forget|ignore) (everything|all)",
    r"jailbreak", r"\bDAN\b", r"do anything now",
    r"pretend to be", r"roleplay as", r"act as if you",
    r"reveal (your |the )?(system|hidden|internal) (prompt|instruction)",
    r"output (your |the )?(system|hidden) (prompt|instruction|config)",
]


@dataclass
class Turn:
    role: str  # "user" | "assistant"
    text: str


@dataclass
class Session:
    session_id: str
    turns: list[Turn] = field(default_factory=list)

    def add(self, role: str, text: str) -> None:
        self.turns.append(Turn(role, text))
        if len(self.turns) > _MAX_TURNS:
            self.turns = self.turns[-_MAX_TURNS:]


class SessionStore:
    """In-memory session store (per replica). Multi-replica fleets should back this with
    Redis; the interface is intentionally tiny so it can be swapped without touching callers."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        # RLock: append() acquires the lock then delegates to get_or_create(), which
        # re-acquires it — reentrancy required to avoid self-deadlock on one thread.
        self._lock = threading.RLock()

    def get_or_create(self, session_id: str) -> Session:
        with self._lock:
            return self._sessions.setdefault(session_id, Session(session_id))

    def append(self, session_id: str, role: str, text: str) -> None:
        with self._lock:
            self.get_or_create(session_id).add(role, text)

    def history(self, session_id: str) -> list[Turn]:
        with self._lock:
            s = self._sessions.get(session_id)
            return list(s.turns) if s else []


_sessions = SessionStore()


def get_session_store() -> SessionStore:
    return _sessions


_REFERENCE_RE = re.compile(
    r"\b(it|its|they|them|their|this|that|these|those|the (plan|price|cost|fee|subscription|service|product|feature|policy|document|page))\b",
    re.IGNORECASE,
)


def _heuristic_rewrite(history: list[Turn], question: str) -> str:
    """Deterministic fallback: if the question relies on prior context (weak references) and
    we have prior turns, prepend a compact context summary so the embedder sees the topic."""
    if not history:
        return question
    if not _REFERENCE_RE.search(question):
        return question
    # last assistant answer (most recent "anchor" of the topic) + last user question.
    recent = [t for t in history if t.role == "user"][-2:] + [t for t in history if t.role == "assistant"][-1:]
    ctx = " | ".join(f"{t.role}: {t.text}" for t in recent if t.text)
    if not ctx:
        return question
    return f"[Context: {ctx}] {question}"


def rewrite_query(session_id: str | None, question: str) -> tuple[str, bool]:
    """Return (rewritten_query, was_rewritten). Uses LLM rewrite when configured, else the
    deterministic heuristic. Never throws — falls back to the original question."""
    if not session_id:
        return question, False
    history = get_session_store().history(session_id)
    if not history:
        return question, False

    s = get_settings()
    # LLM-backed rewrite when generation is configured.
    if s.llm_base_url and s.llm_api_key and s.llm_model:
        try:
            return _llm_rewrite(history, question, s), True
        except Exception:  # noqa: BLE001,S110 - fall back to heuristic on any provider error
            pass
    rewritten = _heuristic_rewrite(history, question)
    return rewritten, rewritten != question


def _llm_rewrite(history: list[Turn], question: str, s) -> str:
    import httpx

    chat = [{"role": "user" if t.role == "user" else "assistant", "content": t.text}
            for t in history]
    chat.append({"role": "user", "content":
        "Rewrite the user's latest question to be fully self-contained and unambiguous for a "
        "document search, resolving any pronouns or references using the conversation above. "
        "Output ONLY the rewritten question, no explanation.\n\nLatest question: " + question})
    resp = httpx.post(
        s.llm_base_url.rstrip("/") + "/chat/completions",
        headers={"Authorization": f"Bearer {s.llm_api_key}", "Content-Type": "application/json"},
        json={"model": s.llm_model, "messages": chat, "temperature": 0.0},
        timeout=20,
    )
    resp.raise_for_status()
    text = resp.json()["choices"][0]["message"]["content"].strip().strip('"')
    return text or question


_INJ_RE = [re.compile(p, re.IGNORECASE) for p in _INJECTION_PATTERNS]


def detect_injection(text: str) -> bool:
    """Heuristic injection/jailbreak detection (PromptGuard-style input-layer guard)."""
    return any(rx.search(text) for rx in _INJ_RE)


def assess_confidence(hits: list[dict], threshold: float) -> tuple[bool, str]:
    """Return (in_scope, reason). in_scope=False means abstain (graceful handoff)."""
    if not hits:
        return False, "no_context"
    top = hits[0].get("rerank_score", hits[0].get("score", 0.0))
    if top < threshold:
        return False, "low_retrieval_confidence"
    return True, "ok"
