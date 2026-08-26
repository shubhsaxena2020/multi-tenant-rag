"""Embedding interface.

Real impl (BGE-M3 via sentence-transformers) is lazy-imported so importing this
module never requires the multi-GB model. DeterministicEmbedder hashes text into a
unit vector — used for tests and no-model environments, exercising the FULL pipeline
(chunker -> embed -> Qdrant -> query -> rerank) with isolation guarantees verifiable
without downloading models.
"""
from __future__ import annotations

import hashlib
import math
from abc import ABC, abstractmethod

import numpy as np

from ..config import Settings, get_settings


class Embedder(ABC):
    dim: int

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]:
        return self.embed([text])[0]


class DeterministicEmbedder(Embedder):
    """Stable, deterministic pseudo-embedding for tests. NOT for production quality."""

    def __init__(self, dim: int = 1024, seed: int = 0):
        self.dim = dim
        self._seed = seed

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            h = hashlib.sha256((f"{self._seed}:{t}").encode()).digest()
            vec = np.frombuffer(h, dtype=np.uint8).astype(np.float32).tolist()
            # expand to dim
            base = vec * (self.dim // len(vec) + 1)
            vec = base[: self.dim]
            norm = math.sqrt(sum(x * x for x in vec)) or 1.0
            out.append([x / norm for x in vec])
        return out


class BgeM3Embedder(Embedder):
    def __init__(self, model_name: str, device: str, dim: int = 1024):
        from sentence_transformers import SentenceTransformer

        self.dim = dim
        self._model = SentenceTransformer(model_name, device=device)

    def embed(self, texts: list[str]) -> list[list[float]]:
        # BGE-M3 returns dense+sparse+colbert; take dense representation.
        out = self._model.encode(
            texts, normalize_embeddings=True, convert_to_numpy=True, pooling="cls"
        )
        return [v.tolist() for v in out]


def get_embedder(settings: Settings | None = None) -> Embedder:
    settings = settings or get_settings()
    if settings.use_real_embedder:
        return _real_embedder(settings)
    return _deterministic_embedder(settings)


_embedder_cache: dict[str, Embedder] = {}


def _deterministic_embedder(settings: Settings) -> Embedder:
    key = f"det:{settings.vector_size}"
    if key not in _embedder_cache:
        _embedder_cache[key] = DeterministicEmbedder(settings.vector_size)
    return _embedder_cache[key]


def _real_embedder(settings: Settings) -> Embedder:
    key = f"real:{settings.embed_model}:{settings.embed_device}"
    if key not in _embedder_cache:
        # Import heavy deps lazily; cache the model so it loads once, not per request.
        _embedder_cache[key] = BgeM3Embedder(
            settings.embed_model, settings.embed_device, settings.vector_size
        )
    return _embedder_cache[key]
