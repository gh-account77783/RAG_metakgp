"""Bounded rank fusion and source diversity, independent of engine libraries."""
from collections import defaultdict
from collections.abc import Sequence


def reciprocal_rank_fusion(*rankings: Sequence[str], limit: int = 50, k: int = 60) -> list[str]:
    scores = defaultdict(float)
    for ranking in rankings:
        seen = set()
        for rank, identifier in enumerate(ranking, 1):
            if identifier not in seen:
                scores[identifier] += 1.0 / (k + rank)
                seen.add(identifier)
    return sorted(scores, key=lambda value: (-scores[value], value))[:limit]
