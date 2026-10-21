"""Compatibility imports for the shared embedding adapters."""
from .rag.components.embeddings import (
    EmbeddingProvider, HashEmbedding, OllamaEmbedding, cosine_similarity,
)

__all__ = ["EmbeddingProvider", "HashEmbedding", "OllamaEmbedding", "cosine_similarity"]
