"""Small deterministic embeddings used by the first local slice.

The implementation intentionally avoids model downloads during installation and tests.
P4 can replace it behind the same adapter after measuring a selected embedding model.
"""

from __future__ import annotations

import hashlib
import math
import re


class HashEmbedding:
    algorithm = "graphmind-hash-embedding-v1"

    def __init__(self, dimension: int = 384) -> None:
        if dimension < 32:
            raise ValueError("Embedding dimension must be at least 32")
        self.dimension = dimension

    @property
    def fingerprint(self) -> str:
        return f"{self.algorithm}:{self.dimension}"

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        tokens = re.findall(r"[\w'-]+", text.casefold(), flags=re.UNICODE)
        for token in tokens:
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=16).digest()
            first = int.from_bytes(digest[:8], "big")
            second = int.from_bytes(digest[8:], "big")
            vector[first % self.dimension] += 1.0 if second & 1 else -1.0
        magnitude = math.sqrt(sum(value * value for value in vector))
        return vector if magnitude == 0 else [value / magnitude for value in vector]


def cosine_similarity(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))

