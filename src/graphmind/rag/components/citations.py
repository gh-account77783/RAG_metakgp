"""Select provider citations from original retrieved evidence."""
from __future__ import annotations

from collections.abc import Sequence

from ...errors import ProviderResponseError
from .contracts import Evidence, Outcome, ProviderAnswer


def citations_for_answer(
    answer: ProviderAnswer, evidence: Sequence[Evidence]
) -> tuple[Evidence, ...]:
    """Validate membership, not factual truth."""
    allowed = {item.citation_id: item for item in evidence}
    if answer.outcome is Outcome.ANSWER:
        if not answer.citation_ids or any(item not in allowed for item in answer.citation_ids):
            raise ProviderResponseError("Provider citations do not match retrieved evidence")
        return tuple(allowed[item] for item in answer.citation_ids)
    return ()
