"""Local meeting search + grounded RAG.

The relevance index is self-contained and offline. It requires no model
download or network access.
"""
from core.search.embeddings import (
    EmbeddingEngine, EmbeddingManager, HashingEmbedder,
)
from core.search.hybrid import hybrid_search
from core.search.rag import build_context, rag_answer
from core.search.vectorstore import embed_meeting, vector_search

__all__ = [
    "EmbeddingEngine", "EmbeddingManager", "HashingEmbedder",
    "hybrid_search", "build_context", "rag_answer",
    "embed_meeting", "vector_search",
]
