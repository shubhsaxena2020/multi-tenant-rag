"""Reranking interface.

Two implementations:
- CrossEncoderReranker: real BGE-Reranker-v2-m3 via sentence-transformers (or HF TEI).
  Lazy-imported. +5..15 NDCG@10 over bi-encoder alone (LocalAI Master 2026).
- ScoreReranker: deterministic fallback that re-sorts by the vector similarity score.
  Used when the reranker is disabled/unavailable so the service degrades gracefully.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..config import Settings, get_settings


class Reranker(ABC):
    @abstractmethod
    def rerank(self, query: str, items: list[dict]) -> list[dict]:
        """items: list of {text, score, ...}. Returns re-sorted list (best first)."""
        ...


class ScoreReranker(Reranker):
    """No-op rerank: keep vector-similarity ordering (graceful fallback)."""

    def rerank(self, query: str, items: list[dict]) -> list[dict]:
        return sorted(items, key=lambda x: x.get("score", 0.0), reverse=True)


class FlashRankReranker(Reranker):
    """Real reranker via `rerankers` FlashRank backend (ms-marco-MiniLM, CPU, no torch).

    Lightweight enough for a CPU VPS fleet — closes the gap where the platform previously
    only offered a deterministic score-sort fallback. Returns the same contract as the
    other rerankers (items re-sorted best-first with `rerank_score`).
    """

    def __init__(self, verbose: bool = False):
        try:
            from rerankers import Reranker as _R
        except ImportError as e:  # pragma: no cover - optional dep
            raise RuntimeError(
                "rerankers is required for the real reranker (USE_REAL_RERANKER=1). "
                "Install with: uv pip install 'rerankers[flashrank]'"
            ) from e
        self._rk = _R("flashrank", verbose=verbose)

    def rerank(self, query: str, items: list[dict]) -> list[dict]:
        if not items:
            return items
        texts = [it["text"] for it in items]
        out = self._rk.rank(query=query, docs=texts)
        # rerankers returns RankedResults; map docid/order back to original items.
        ranked = getattr(out, "results", list(out))
        scored = []
        for rank_pos, r in enumerate(ranked):
            # find original item by matching text (stable for distinct texts)
            idx = next(i for i, it in enumerate(items) if it["text"] == r.text)
            scored.append(dict(items[idx], rerank_score=float(r.score), _pos=rank_pos))
        return sorted(scored, key=lambda x: x["rerank_score"], reverse=True)


class CrossEncoderReranker(Reranker):
    def __init__(self, model_name: str, device: str = "cpu"):
        from sentence_transformers import CrossEncoder

        self._model = CrossEncoder(model_name, device=device)

    def rerank(self, query: str, items: list[dict]) -> list[dict]:
        if not items:
            return items
        pairs = [(query, it["text"]) for it in items]
        scores = self._model.predict(pairs, batch_size=32)
        scored = [dict(it, rerank_score=float(s)) for it, s in zip(items, scores)]
        return sorted(scored, key=lambda x: x["rerank_score"], reverse=True)


def get_reranker(settings: Settings | None = None) -> Reranker:
    settings = settings or get_settings()
    if settings.use_real_reranker:
        return _real_reranker(settings)
    return _score_reranker()


_reranker_cache: dict[str, Reranker] = {}


def _score_reranker() -> Reranker:
    if "score" not in _reranker_cache:
        _reranker_cache["score"] = ScoreReranker()
    return _reranker_cache["score"]


def _real_reranker(settings: Settings) -> Reranker:
    if settings.rerank_provider == "sentence_transformers":
        key = f"ce:{settings.rerank_model}:{settings.rerank_device}"
        if key not in _reranker_cache:
            _reranker_cache[key] = CrossEncoderReranker(
                settings.rerank_model, settings.rerank_device
            )
        return _reranker_cache[key]

    # default real path: flashrank (lightweight CPU, no torch)
    key = "flashrank"
    if key not in _reranker_cache:
        _reranker_cache[key] = FlashRankReranker()
    return _reranker_cache[key]
