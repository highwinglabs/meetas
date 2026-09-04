"""Offline text indexing for local meeting search.

The index deliberately uses a small, deterministic lexical vectorizer. It has
no model download, no network access and no optional provider to configure.
"""
from __future__ import annotations

import hashlib
import re
from abc import ABC, abstractmethod

import numpy as np

from core.config import Config

# ``unicode61`` is the lexical search tokenizer used by FTS5.  Keep the local
# vectorizer's token boundaries compatible with it instead of silently
# dropping German (or any other non-ASCII) letters such as ``ä``/``ß``.
_WORD_RE = re.compile(r"[^\W_]+(?:'[^\W_]+)?", re.UNICODE)


def _tokenize(text: str) -> list[str]:
    """Lower-cased words, word pairs and character trigrams."""
    words = _WORD_RE.findall((text or "").lower())
    grams: list[str] = []
    grams.extend(words)
    grams.extend(a + " " + b for a, b in zip(words, words[1:]))
    for word in words:
        if len(word) >= 3:
            grams.extend(word[i:i + 3] for i in range(len(word) - 2))
    return grams


class EmbeddingEngine(ABC):
    """Local text-to-vector interface used by the relevance index."""

    name: str = "abstract"
    dim: int = 0

    @abstractmethod
    def is_ready(self) -> bool:
        """Whether the engine can be used immediately."""

    @abstractmethod
    def prepare(self, allow_download: bool = False) -> None:
        """Prepare the engine; the local engine never downloads anything."""

    @abstractmethod
    def embed(self, texts: list[str]) -> list[np.ndarray]:
        """Return one normalized vector per text."""

    def embed_one(self, text: str) -> np.ndarray:
        return self.embed([text])[0]


class HashingEmbedder(EmbeddingEngine):
    """Dependency-free lexical vectorizer for offline relevance ranking."""

    def __init__(self, dim: int = 256) -> None:
        self.dim = max(16, int(dim))
        self.name = f"hashing-{self.dim}"

    def is_ready(self) -> bool:
        return True

    def prepare(self, allow_download: bool = False) -> None:
        return None

    def _bucket(self, token: str, seed: int) -> int:
        digest = hashlib.blake2b(
            f"{seed}|{token}".encode("utf-8"), digest_size=8).digest()
        return int.from_bytes(digest, "little")

    def _vector(self, text: str) -> np.ndarray:
        vector = np.zeros(self.dim, dtype=np.float32)
        for gram in _tokenize(text):
            index = self._bucket(gram, 0) % self.dim
            vector[index] += 1.0 if self._bucket(gram, 1) % 2 == 0 else -1.0
        norm = float(np.linalg.norm(vector))
        if norm > 0:
            vector /= norm
        return vector

    def embed(self, texts: list[str]) -> list[np.ndarray]:
        return [self._vector(text) for text in texts]


class EmbeddingManager:
    """Provide the fixed offline relevance engine used by search and RAG."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._engine: EmbeddingEngine | None = None

    def engine(self) -> EmbeddingEngine:
        if self._engine is None:
            self._engine = HashingEmbedder(self.config.embedding_dim)
        return self._engine

    def clear_cache(self) -> None:
        self._engine = None

    def is_enabled(self) -> bool:
        return bool(self.config.embeddings_enabled)

    def backend_status(self) -> dict:
        engine = self.engine()
        return {
            "enabled": self.is_enabled(),
            "backend": "offline-local",
            "active_backend": engine.name,
            "dim": int(engine.dim),
        }
