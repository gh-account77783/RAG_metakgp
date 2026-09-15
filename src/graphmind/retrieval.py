"""Active-manifest vector/graph retrieval and cited answer validation."""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Sequence

from .domain import Evidence, Outcome, QueryResult
from .errors import (
    EmbeddingMismatchError,
    GraphMindError,
    ProviderResponseError,
    QueryBudgetError,
    QueryCancelledError,
    QueryCapacityError,
    RetryableQueryError,
)
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
        seed_limit: int = 4,
        graph_limit: int = 8,
        graph_scope: str = "chunk",
        max_evidence: int = 12,
        min_vector_score: float = 0.02,
        max_question_chars: int = 2000,
    ) -> None:
        self.metadata = metadata
        self.vector = vector
        self.graph = graph
        self.installation_id = installation_id
        self.seed_limit = seed_limit
        self.graph_limit = graph_limit
        self.graph_scope = graph_scope
        self.max_evidence = max_evidence
        self.min_vector_score = min_vector_score
        self.max_question_chars = max_question_chars

    def retrieve(self, query: str) -> list[Evidence]:
        if not query.strip():
            return []
        if len(query) > self.max_question_chars:
            raise QueryBudgetError("Question exceeds the configured character budget")
        active_fingerprints = self.metadata.active_embedding_fingerprints()
        incompatible = active_fingerprints - {self.vector.embedding_fingerprint}
        if incompatible:
            raise EmbeddingMismatchError(
                "Active documents use a different embedding fingerprint; reindex them explicitly"
            )
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
        graph_ids = self.graph.expand(
            self.installation_id,
            seed_ids,
            self.graph_limit,
            self.graph_scope,
        )
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
    def __init__(
        self,
        metadata: MetadataStore,
        retrieval: RetrievalService,
        provider: AnswerProvider,
        *,
        max_concurrent_queries: int = 4,
        max_queued_queries: int = 8,
        queue_timeout: float = 1.0,
    ) -> None:
        self.metadata = metadata
        self.retrieval = retrieval
        self.provider = provider
        self.queue_timeout = queue_timeout
        self.max_queued_queries = max_queued_queries
        self._capacity = threading.BoundedSemaphore(max_concurrent_queries)
        self._waiters = 0
        self._waiters_lock = threading.Lock()

    def _acquire_capacity(self) -> bool:
        if self._capacity.acquire(blocking=False):
            return True
        with self._waiters_lock:
            if self._waiters >= self.max_queued_queries:
                return False
            self._waiters += 1
        try:
            return self._capacity.acquire(timeout=self.queue_timeout)
        finally:
            with self._waiters_lock:
                self._waiters -= 1

    def _answer_with_evidence(
        self,
        question: str,
        evidence: Sequence[Evidence],
        request_id: str,
        started: float,
        provider_calls_before: int,
        cancel_event: threading.Event | None,
    ) -> QueryResult:
        if not evidence:
            return QueryResult(
                outcome=Outcome.INSUFFICIENT_EVIDENCE,
                answer="I don't know based on the provided knowledge set.",
                citations=(),
                request_id=request_id,
                verification={
                    "citations_valid": True,
                    "evidence_count": 0,
                    "provider_calls": 0,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                },
            )
        if cancel_event is not None and cancel_event.is_set():
            raise QueryCancelledError("Query was cancelled")
        answer = self.provider.answer(question, evidence)
        if cancel_event is not None and cancel_event.is_set():
            raise QueryCancelledError("Query was cancelled")
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
        provider_calls_after = int(getattr(self.provider, "call_count", provider_calls_before))
        return QueryResult(
            outcome=answer.outcome,
            answer=answer.answer,
            citations=citations,
            request_id=request_id,
            verification={
                "citations_valid": True,
                "evidence_count": len(evidence),
                "cited_count": len(citations),
                "provider_calls": max(0, provider_calls_after - provider_calls_before),
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            },
        )

    def query_with_evidence(
        self, question: str, *, cancel_event: threading.Event | None = None
    ) -> tuple[QueryResult, tuple[Evidence, ...]]:
        request_id = str(uuid.uuid4())
        started = time.perf_counter()
        if cancel_event is not None and cancel_event.is_set():
            error = QueryCancelledError("Query was cancelled")
            return (
                QueryResult(
                    outcome=Outcome.ERROR,
                    answer="The query was cancelled.",
                    citations=(),
                    request_id=request_id,
                    verification={
                        "citations_valid": False,
                        "error_code": error.code,
                        "provider_calls": 0,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                    },
                ),
                (),
            )
        acquired = self._acquire_capacity()
        if not acquired:
            error = QueryCapacityError("Query capacity is full")
            return (
                QueryResult(
                    outcome=Outcome.ERROR,
                    answer="A required service is unavailable.",
                    citations=(),
                    request_id=request_id,
                    verification={
                        "citations_valid": False,
                        "error_code": error.code,
                        "provider_calls": 0,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                    },
                ),
                (),
            )
        evidence: tuple[Evidence, ...] = ()
        provider_calls_before = int(getattr(self.provider, "call_count", 0))
        try:
            if cancel_event is not None and cancel_event.is_set():
                raise QueryCancelledError("Query was cancelled")
            evidence = tuple(self.retrieval.retrieve(question))
            result = self._answer_with_evidence(
                question,
                evidence,
                request_id,
                started,
                provider_calls_before,
                cancel_event,
            )
            return result, evidence
        except RetryableQueryError:
            raise
        except GraphMindError as exc:
            provider_calls_after = int(
                getattr(self.provider, "call_count", provider_calls_before)
            )
            return (
                QueryResult(
                    outcome=Outcome.ERROR,
                    answer=(
                        "The query was cancelled."
                        if isinstance(exc, QueryCancelledError)
                        else "A required service is unavailable."
                    ),
                    citations=(),
                    request_id=request_id,
                    verification={
                        "citations_valid": False,
                        "error_code": exc.code,
                        "provider_calls": max(
                            0, provider_calls_after - provider_calls_before
                        ),
                        "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                    },
                ),
                evidence,
            )
        finally:
            self._capacity.release()

    def query(
        self, question: str, *, cancel_event: threading.Event | None = None
    ) -> QueryResult:
        result, _ = self.query_with_evidence(question, cancel_event=cancel_event)
        return result


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
