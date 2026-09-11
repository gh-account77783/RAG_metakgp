"""Stable domain contracts shared by persistence and interfaces."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class VersionStatus(StrEnum):
    STAGED = "staged"
    INDEXING = "indexing"
    READY = "ready"
    FAILED = "failed"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class Outcome(StrEnum):
    ANSWER = "answer"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class Document:
    document_id: str
    collection_id: str
    display_name: str
    normalized_name: str
    media_type: str
    source_key: str
    created_at: str
    active_version_id: str | None = None
    deleted_at: str | None = None


@dataclass(frozen=True, slots=True)
class Version:
    version_id: str
    document_id: str
    content_hash: str
    extractor_fingerprint: str
    chunker_fingerprint: str
    embedding_fingerprint: str
    private_file_ref: str
    source_size: int
    expected_chunk_count: int
    chunk_digest: str
    status: VersionStatus
    created_at: str
    failure_code: str | None = None


@dataclass(frozen=True, slots=True)
class Chunk:
    chunk_id: str
    version_id: str
    document_id: str
    ordinal: int
    text: str
    locator: str
    start_offset: int
    end_offset: int
    reference_names: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Job:
    job_id: str
    idempotency_key: str
    operation: str
    document_id: str
    version_id: str
    status: JobStatus
    attempt_count: int
    available_at: str
    lease_owner: str | None = None
    lease_expires_at: str | None = None
    heartbeat_at: str | None = None
    progress: int = 0
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class SearchHit:
    chunk_id: str
    score: float


@dataclass(frozen=True, slots=True)
class Evidence:
    citation_id: str
    document_id: str
    version_id: str
    display_name: str
    locator: str
    excerpt: str
    via: str
    score: float


@dataclass(frozen=True, slots=True)
class ProviderAnswer:
    outcome: Outcome
    answer: str
    citation_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class QueryResult:
    outcome: Outcome
    answer: str
    citations: tuple[Evidence, ...]
    request_id: str
    verification: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "answer": self.answer,
            "citations": [
                {
                    "citation_id": item.citation_id,
                    "document_id": item.document_id,
                    "version_id": item.version_id,
                    "display_name": item.display_name,
                    "locator": item.locator,
                    "excerpt": item.excerpt,
                    "via": item.via,
                    "score": item.score,
                }
                for item in self.citations
            ],
            "request_id": self.request_id,
            "verification": dict(self.verification),
        }


@dataclass(frozen=True, slots=True)
class AdapterStatus:
    name: str
    available: bool
    detail: str

