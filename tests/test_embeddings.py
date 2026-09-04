"""Regression tests for the dependency-free local relevance index."""
from __future__ import annotations

from core.search.embeddings import EmbeddingManager, HashingEmbedder


def test_hashing_is_the_only_backend(config) -> None:
    manager = EmbeddingManager(config)
    status = manager.backend_status()
    assert status["enabled"] is True
    assert status["backend"] == "offline-local"
    assert status["active_backend"].startswith("hashing")
    assert manager.engine().is_ready() is True


def test_hashing_embedder_is_stable_and_dimensioned(config) -> None:
    engine = HashingEmbedder(config.embedding_dim)
    first = engine.embed_one("hallo welt")
    second = engine.embed_one("hallo welt")
    assert first.shape == (config.embedding_dim,)
    assert first.dtype == second.dtype
    assert (first == second).all()
