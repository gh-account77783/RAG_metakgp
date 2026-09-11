"""Active-manifest vector/graph retrieval and cited answer validation."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from .domain import Evidence, Outcome, QueryResult
from .errors import GraphMindError, ProviderResponseError, RetryableQueryError
from .metadata import MetadataStore
from .providers import AnswerProvider
from .storage import GraphStore, VectorStore


class RetrievalService:
    def __init__(
        self,
        metadata: MetadataStore,
        vector: VectorStore,
        graph: GraphStore,
        installation_id: str,
        *,
        seed_limit: int = 2,
        graph_limit: int = 6,
        max_evidence: int = 8,
        min_vector_score: float = 0.02,
    ) -> None:
        self.metadata = metadata
        self.vector = vector
        self.graph = graph
        self.installation_id = installation_id
        self.seed_limit = seed_limit
        self.graph_limit = graph_limit
        self.max_evidence = max_evidence
        self.min_vector_score = min_vector_score

    def retrieve(self, query: str) -> list[Evidence]:
        if not query.strip() or len(query) > 2000:
            return []
        hits = self.vector.search(
            self.installation_id,
            query,
            limit=max(self.seed_limit * 6, self.max_evidence),
        )
        hit_ids = [hit.chunk_id for hit in hits]
        active_hits = self.metadata.active_chunks(hit_ids)
        eligible_hits = [
            hit
            for hit in hits
            if hit.chunk_id in active_hits and hit.score >= self.min_vector_score
        ][: self.seed_limit]
        if not eligible_hits:
            return []

        seed_ids = [hit.chunk_id for hit in eligible_hits]
        graph_ids = self.graph.expand(self.installation_id, seed_ids, self.graph_limit)
        graph_active = self.metadata.active_chunks(graph_ids)
        evidence: list[Evidence] = []
        seen: set[str] = set()
        score_by_id = {hit.chunk_id: hit.score for hit in eligible_hits}
        for via, candidate_ids in (("vector", seed_ids), ("graph", graph_ids)):
            records = active_hits if via == "vector" else graph_active
            for chunk_id in candidate_ids:
                if chunk_id in seen or chunk_id not in records:
                    continue
                chunk, document = records[chunk_id]
                evidence.append(
                    Evidence(
                        citation_id=chunk.chunk_id,
                        document_id=chunk.document_id,
                        version_id=chunk.version_id,
                        display_name=document.display_name,
                        locator=chunk.locator,
                        excerpt=chunk.text[:800],
                        via=via,
                        score=score_by_id.get(chunk_id, 0.0),
                    )
                )
                seen.add(chunk_id)
                if len(evidence) >= self.max_evidence:
                    return evidence
        return evidence


class AnswerService:
    def __init__(self, metadata: MetadataStore, retrieval: RetrievalService, provider: AnswerProvider) -> None:
        self.metadata = metadata
        self.retrieval = retrieval
        self.provider = provider

    def query(self, question: str) -> QueryResult:
        request_id = str(uuid.uuid4())
        try:
            evidence = self.retrieval.retrieve(question)
            if not evidence:
                return QueryResult(
                    outcome=Outcome.INSUFFICIENT_EVIDENCE,
                    answer="I don't know based on the provided knowledge set.",
                    citations=(),
                    request_id=request_id,
                    verification={"citations_valid": True, "evidence_count": 0},
                )
            answer = self.provider.answer(question, evidence)
            allowed = {item.citation_id: item for item in evidence}
            if answer.outcome is Outcome.ANSWER:
                if not answer.citation_ids or any(item not in allowed for item in answer.citation_ids):
                    raise ProviderResponseError("Provider citations do not match retrieved evidence")
                citations = tuple(allowed[item] for item in answer.citation_ids)
            else:
                citations = ()
            active_versions = self.metadata.active_version_ids()
            if any(item.version_id not in active_versions for item in citations):
                raise RetryableQueryError("A cited version changed while the answer was generated")
            return QueryResult(
                outcome=answer.outcome,
                answer=answer.answer,
                citations=citations,
                request_id=request_id,
                verification={
                    "citations_valid": True,
                    "evidence_count": len(evidence),
                    "cited_count": len(citations),
                },
            )
        except RetryableQueryError:
            raise
        except GraphMindError as exc:
            return QueryResult(
                outcome=Outcome.ERROR,
                answer="A required service is unavailable.",
                citations=(),
                request_id=request_id,
                verification={"citations_valid": False, "error_code": exc.code},
            )


class LegacyReasonAdapter:
    """Preserve the legacy reason(query) facade while the public adapters migrate."""

    def __init__(self, answer_service: AnswerService) -> None:
        self.answer_service = answer_service

    def reason(self, query: str) -> dict[str, object]:
        result = self.answer_service.query(query)
        return {
            "answer": result.answer,
            "path": [item.display_name for item in result.citations],
            "knowledge": "\n\n".join(item.excerpt for item in result.citations),
            "verification": {
                **result.verification,
                "outcome": result.outcome.value,
                "request_id": result.request_id,
                "citations": [item.citation_id for item in result.citations],
            },
        }
