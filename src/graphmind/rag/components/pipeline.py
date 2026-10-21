"""Shared admission, answer generation and active-source validation."""
from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Sequence

from ...errors import (
    GraphMindError, QueryCancelledError, QueryCapacityError, RetryableQueryError,
)
from ...metadata import MetadataStore
from ...providers import AnswerProvider
from ...sources import SourceService
from .citations import citations_for_answer
from .contracts import Evidence, EvidenceRetriever, Outcome, QueryResult


class AnswerService:
    def __init__(
        self,
        metadata: MetadataStore,
        retrieval: EvidenceRetriever,
        provider: AnswerProvider,
        *,
        max_concurrent_queries: int = 4,
        max_queued_queries: int = 8,
        queue_timeout: float = 1.0,
        sources: SourceService | None = None,
    ) -> None:
        self.metadata = metadata
        self.retrieval = retrieval
        self.provider = provider
        self.sources = sources or SourceService(metadata)
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
        if cancel_event is not None and cancel_event.is_set():
            raise QueryCancelledError("Query was cancelled")
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
        self.sources.assert_current(item.version_id for item in evidence)
        answer = self.provider.answer(question, evidence)
        if cancel_event is not None and cancel_event.is_set():
            raise QueryCancelledError("Query was cancelled")
        citations = citations_for_answer(answer, evidence)
        # All context can influence the answer, including passages not cited by
        # the provider. Recheck the complete input snapshot before delivery.
        self.sources.assert_current(item.version_id for item in evidence)
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

    def search(self, question: str, *, cancel_event: threading.Event | None = None) -> list[Evidence]:
        """Share answer admission limits with client-side synthesis searches."""
        if cancel_event is not None and cancel_event.is_set():
            raise QueryCancelledError("Query was cancelled")
        if not self._acquire_capacity():
            raise QueryCapacityError("Query capacity is full")
        try:
            if cancel_event is not None and cancel_event.is_set():
                raise QueryCancelledError("Query was cancelled")
            evidence = self.retrieval.retrieve(question)
            if cancel_event is not None and cancel_event.is_set():
                raise QueryCancelledError("Query was cancelled")
            self.sources.assert_current(item.version_id for item in evidence)
            return evidence
        finally:
            self._capacity.release()

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
