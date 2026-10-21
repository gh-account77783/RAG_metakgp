"""Small text utilities shared by ingestion and retrieval code."""

from typing import List


def chunk_text(text: str, chunk_size: int = 1600, overlap: int = 200) -> List[str]:
    """Split text into deterministic overlapping character windows."""
    if not text:
        return []
    if chunk_size <= 0:
        raise ValueError("chunk_size must be greater than zero")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap must be non-negative and smaller than chunk_size")

    chunks = []
    start = 0
    while start < len(text):
        chunks.append(text[start : start + chunk_size])
        start += chunk_size - overlap
    return chunks
