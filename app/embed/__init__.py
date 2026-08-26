"""Embedding interface — hybrid (dense + sparse) capable.

Design goals (2026 research, see RESEARCH-2026.md §3/§4):
- BGE-M3 emits DENSE + SPARSE + MULTI-VECTOR in one pass. We use dense (for ANN
  recall) and sparse (lexical/BM25-style) so retrieval can fuse them (RRF) for the
  +8–14 recall@10 hybrid lift (Agile Infoways 2026).
- Models are lazy-loaded and cached (singleton) so a 2 GB model loads once, not per
  request — essential at fleet scale.
- A deterministic embedder (hash → unit dense + hashed sparse) exercises the FULL
  hybrid pipeline (chunk → embed → Qdrant sparse+dense → query → RRF → rerank) with
  zero model downloads, so tests verify isolation + hybrid wiring without GB downloads.
- Optional TEI (Text Embeddings Inference) URL provider: point EMBED_BASE_URL at a
  self-hosted TEI GPU node (DEPLOYMENT.md) to offload embedding from app replicas.

The `Embedder` contract returns `EmbedResult(dense, sparse)` where sparse is a dict of
{token_id: weight}. Qdrant stores sparse vectors natively.
"""
from __future__ import annotations

import hashlib
import math
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import numpy as np

from ..config import Settings, get_settings

# A sparse vector is {int token_id: float weight}
SparseVec = dict[int, float]


@dataclass
class EmbedResult:
    dense: list[float]
    sparse: SparseVec = field(default_factory=dict)


class Embedder(ABC):
    dim: int

    @abstractmethod
    def embed(self, texts: list[str]) -> list[EmbedResult]: ...

    def embed_query(self, text: str) -> EmbedResult:
        return self.embed([text])[0]


class DeterministicEmbedder(Embedder):
    """Stable deterministic hybrid embedder for tests/CI. NOT production quality.

    dense = hash→unit vector (as before). sparse = hashed bag-of-words token ids so the
    RRF fusion path is exercised end-to-end without a real model.
    """

    def __init__(self, dim: int = 1024, seed: int = 0, vocab: int = 30_000):
        self.dim = dim
        self._seed = seed
        self._vocab = vocab

    def _dense(self, t: str) -> list[float]:
        h = hashlib.sha256(f"{self._seed}:{t}".encode()).digest()
        raw = np.frombuffer(h, dtype=np.uint8).astype(np.float32)
        base = np.tile(raw, self.dim // len(raw) + 1)[: self.dim]
        norm = float(np.linalg.norm(base)) or 1.0
        return (base / norm).tolist()

    def _sparse(self, t: str) -> SparseVec:
        out: SparseVec = {}
        for tok in t.lower().split():
            tid = int(hashlib.md5(tok.encode()).hexdigest(), 16) % self._vocab
            out[tid] = out.get(tid, 0.0) + 1.0
        # l2-ish normalize weights
        n = math.sqrt(sum(v * v for v in out.values())) or 1.0
        return {k: v / n for k, v in out.items()}

    def embed(self, texts: list[str]) -> list[EmbedResult]:
        return [EmbedResult(dense=self._dense(t), sparse=self._sparse(t)) for t in texts]


class BgeM3Embedder(Embedder):
    """Real BGE-M3: returns dense + sparse (lexical) representations."""

    def __init__(self, model_name: str, device: str, dim: int = 1024):
        from sentence_transformers import SentenceTransformer

        self.dim = dim
        self._model = SentenceTransformer(model_name, device=device)
        # BGE-M3 colbert/late-interaction dims differ; we only use dense + sparse.

    def embed(self, texts: list[str]) -> list[EmbedResult]:
        out = self._model.encode(
            texts,
            normalize_embeddings=True,
            convert_to_numpy=True,
            pooling="cls",
            return_sparse=True,  # BGE-M3 native sparse (SPLADE-style)
        )
        results: list[EmbedResult] = []
        dense = np.asarray(out["dense"]) if isinstance(out, dict) else np.asarray(out)
        # sentence-transformers returns a dict when return_sparse=True
        sparse_list = out.get("lexical", []) if isinstance(out, dict) else []
        for i, d in enumerate(dense):
            sp: SparseVec = {}
            if i < len(sparse_list):
                for tok, w in sparse_list[i].items():
                    try:
                        sp[int(tok)] = float(w)
                    except (ValueError, TypeError):
                        continue
            results.append(EmbedResult(dense=d.tolist(), sparse=sp))
        return results


class TeiEmbedder(Embedder):
    """Embedding via a remote Text Embeddings Inference (TEI) endpoint.

    Used when EMBED_BASE_URL is set — offloads the GPU model to a dedicated node so
    stateless app replicas stay light (DEPLOYMENT.md horizontal-scaling path).
    Expects a TEI /embed endpoint returning {"data":[{"embedding":[...]}]} for dense.
    Sparse is approximated client-side (BM25-style hashing) when TEI doesn't return it.
    """

    def __init__(self, base_url: str, model: str, dim: int = 1024, api_key: str = ""):
        import requests

        self.dim = dim
        self._url = base_url.rstrip("/") + "/embed"
        self._model = model
        self._key = api_key
        self._session = requests.Session()
        self._vocab = 30_000

    def _remote_dense(self, texts: list[str]) -> list[list[float]]:
        headers = {"Authorization": f"Bearer {self._key}"} if self._key else {}
        resp = self._session.post(
            self._url,
            json={"model": self._model, "inputs": texts, "normalize": True},
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        return [d["embedding"] for d in data]

    def embed(self, texts: list[str]) -> list[EmbedResult]:
        dense = self._remote_dense(texts)
        out: list[EmbedResult] = []
        for t, d in zip(texts, dense):
            sp: SparseVec = {}
            for tok in t.lower().split():
                tid = int(hashlib.md5(tok.encode()).hexdigest(), 16) % self._vocab
                sp[tid] = sp.get(tid, 0.0) + 1.0
            n = math.sqrt(sum(v * v for v in sp.values())) or 1.0
            out.append(EmbedResult(dense=d, sparse={k: v / n for k, v in sp.items()}))
        return out


_embedder_cache: dict[str, Embedder] = {}
_lock = threading.Lock()


def get_embedder(settings: Settings | None = None) -> Embedder:
    settings = settings or get_settings()
    if settings.use_real_embedder:
        return _real_embedder(settings)
    return _deterministic_embedder(settings)


def _deterministic_embedder(settings: Settings) -> Embedder:
    key = f"det:{settings.vector_size}"
    with _lock:
        if key not in _embedder_cache:
            _embedder_cache[key] = DeterministicEmbedder(settings.vector_size)
        return _embedder_cache[key]


def _real_embedder(settings: Settings) -> Embedder:
    # TEI provider if configured; otherwise in-process BGE-M3 (or Qwen3 path).
    if settings.embed_base_url:
        key = f"tei:{settings.embed_base_url}:{settings.embed_model}"
        with _lock:
            if key not in _embedder_cache:
                _embedder_cache[key] = TeiEmbedder(
                    settings.embed_base_url, settings.embed_model,
                    settings.vector_size, settings.embed_api_key,
                )
            return _embedder_cache[key]
    key = f"real:{settings.embed_model}:{settings.embed_device}"
    with _lock:
        if key not in _embedder_cache:
            _embedder_cache[key] = BgeM3Embedder(
                settings.embed_model, settings.embed_device, settings.vector_size
            )
        return _embedder_cache[key]
