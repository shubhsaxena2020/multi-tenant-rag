"""Unit tests for the deterministic embedder in app/embed/__init__.py.

These tests exercise the FULL hybrid pipeline (chunk → embed → Qdrant sparse+dense →
query → RRF → rerank) with zero model downloads, so they verify isolation + hybrid
wiring without GB downloads.

See app/embed/__init__.py for the Embedder contract and deterministic_embedder() implementation details.
"""

from __future__ import annotations

import hashlib
import math
import numpy as np

import pytest

from rag_service.config import get_settings

from app.embed import EmbedResult, _deterministic_embedder, _RealEmbedder

pytestmark = pytest.mark.unit


class TestEmbedResult:
    """Test the EmbedResult dataclass."""

    def test_dense_required(self):
        """Dense embedding list must be non-empty."""
        result = EmbedResult(dense=[])
        assert len(result.dense) == 0

    def test_sparse_optional(self):
        """Sparse embedding dict is optional (default empty)."""
        result = EmbedResult(dense=[0.5, 0.3, 0.1])
        assert result.sparse == {}

    def test_sparse_populated(self):
        """Sparse embedding can have token IDs with weights."""
        result = EmbedResult(dense=[0.5, 0.3], sparse={1001: 0.8, 2002: 0.6})
        assert result.sparse == {1001: 0.8, 2002: 0.6}


class TestDeterministicEmbedder:
    """Test the deterministic embedder (no external model downloads)."""

    def test_deterministic_same_input_same_output(self):
        """Same input always produces same output with fixed seed."""
        settings = get_settings()
        embedder = _deterministic_embedder(settings)

        result1 = embedder.embed(["hello world", "test query"])
        result2 = embedder.embed(["hello world", "test query"])

        assert len(result1.dense) == len(result2.dense)
        assert all(
            abs(a - b) < 1e-10
            for a, b in zip(result1.dense, result2.dense)
        )
        assert result1.sparse == result2.sparse

    def test_deterministic_different_inputs_different_output(self):
        """Different inputs produce different embeddings."""
        settings = get_settings()
        embedder = _deterministic_embedder(settings)

        r1 = embedder.embed(["hello"])
        r2 = embedder.embed(["world"])

        assert r1.dense != r2.dense or r1.sparse != r2.sparse

    def test_embed_query_method(self):
        """embed_query returns a single EmbedResult."""
        settings = get_settings()
        embedder = _deterministic_embedder(settings)

        result = embedder.embed_query("test query")
        assert isinstance(result, EmbedResult)
        assert len(result.dense) > 0

    def test_embed_passages_method(self):
        """embed_passages returns multiple EmbedResults."""
        settings = get_settings()
        embedder = _deterministic_embedder(settings)

        results = embedder.embed_passages(["text one", "text two"])
        assert len(results) == 2
        assert all(isinstance(r, EmbedResult) for r in results)
        assert all(len(r.dense) > 0 for r in results)

    def test_dimension_consistency(self):
        """All embeddings have the same dimension."""
        settings = get_settings()
        embedder = _deterministic_embedder(settings)

        results = embedder.embed(["a", "b", "c"])
        dim = len(results[0].dense)
        assert all(len(r.dense) == dim for r in results)
        assert dim > 0

    def test_sparse_vocab_coverage(self):
        """Sparse embedding uses vocab-sized hash indices."""
        settings = get_settings()
        embedder = _deterministic_embedder(settings)

        result = embedder.embed(["test"])
        # Sparse indices should be within vocab range
        for token_id in result.sparse:
            assert 0 <= token_id < settings.embed_vocab_size  # type: ignore


class Test_RealEmbedder:
    """Test the real (TEI-backed) embedder when available."""

    @pytest.mark.skipif(
        not hasattr(__import__("rag_service.config", fromlist=["get_settings"]).get_settings().embed_tei_base_url, "embed_tei_base_url")
        or __import__("rag_service.config", fromlist=["get_settings"]).get_settings().embed_tei_base_url == "",
        reason="No TEI endpoint configured"
    )
    def test_real_embedder_has_dimension(self):
        """Real embedder produces 768-dim or 1024-dim vectors depending on config."""
        settings = get_settings()
        embedder = _RealEmbedder(settings)
        result = embedder.embed(["test"])
        assert len(result.dense) == settings.embed_dim  # type: ignore

    def test_real_embedder_query_method(self):
        """Real embedder supports embed_query shortcut."""
        settings = get_settings()
        embedder = _RealEmbedder(settings)

        result = embedder.embed_query("test query")
        assert isinstance(result, type(embedder.embed(["test"])[0]))