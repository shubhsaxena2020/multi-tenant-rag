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
    key = f"real:{settings.rerank_model}:{settings.rerank_device}"
    if key not in _reranker_cache:
        _reranker_cache[key] = CrossEncoderReranker(
            settings.rerank_model, settings.rerank_device
        )
    return _reranker_cache[key]
