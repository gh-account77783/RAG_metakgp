"""Shared source eligibility and original-byte access over the existing authority."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
import hashlib

from .errors import DocumentNotFoundError, RetryableQueryError, SourceIntegrityError
from .files import FileStore
from .metadata import MetadataStore
from .rag.components.contracts import Document, Job, Version, VersionStatus


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    document: Document
    version: Version
    content: bytes = field(repr=False)


class SourceService:
    def __init__(self, metadata: MetadataStore, files: FileStore | None = None,
                 *, max_file_bytes: int = 50 * 1024 * 1024) -> None:
        if max_file_bytes < 1:
            raise ValueError("Original-file byte limit must be positive")
        self.metadata = metadata
        self.files = files
        self.max_file_bytes = max_file_bytes

    def assert_current(self, version_ids: Iterable[str]) -> None:
        selected = set(version_ids)
        if selected and not selected <= self.metadata.active_version_ids():
            raise RetryableQueryError("A source version changed; retry")

    def active(self, document_id: str, version_id: str | None = None) -> tuple[Document, Version]:
        document = self.metadata.document(document_id)
        if document is None or document.deleted_at is not None or document.active_version_id is None:
            raise DocumentNotFoundError("Document is unavailable")
        selected = version_id or document.active_version_id
        version = self.metadata.version(selected)
        if (selected != document.active_version_id or version is None
                or version.document_id != document.document_id
                or version.status.value != VersionStatus.READY.value):
            raise DocumentNotFoundError("Document version is unavailable")
        return document, version

    def _snapshot(self, document: Document, version: Version) -> SourceSnapshot:
        if self.files is None:
            raise SourceIntegrityError("Original-file access is not configured")
        if not 0 <= version.source_size <= self.max_file_bytes:
            raise SourceIntegrityError("Original-file size is unavailable or exceeds its limit")
        # Schema-1 installations have an unknown size (zero after migration).
        # A bounded read and matching raw hash can still authenticate those bytes.
        content = self.files.read_bytes(version.private_file_ref,
                                        max_bytes=version.source_size or self.max_file_bytes)
        if ((version.source_size and len(content) != version.source_size)
                or hashlib.sha256(content).hexdigest() != version.content_hash):
            # Legacy text-derived hashes cannot authenticate byte-sensitive indexes.
            # A host operator must explicitly reimport; never silently rewrite history.
            raise SourceIntegrityError("Original bytes cannot be verified against this version; reimport the source")
        return SourceSnapshot(document, version, content)

    def snapshot(self, document_id: str, version_id: str | None = None) -> SourceSnapshot:
        document, version = self.active(document_id, version_id)
        snapshot = self._snapshot(document, version)
        self.assert_current((version.version_id,))
        return snapshot

    def snapshot_for_job(self, job: Job) -> SourceSnapshot:
        self.metadata.assert_job_lease(job)
        document = self.metadata.document(job.document_id)
        version = self.metadata.version(job.version_id)
        if (document is None or document.deleted_at is not None or version is None
                or version.document_id != document.document_id):
            raise DocumentNotFoundError("Build source is unavailable")
        snapshot = self._snapshot(document, version)
        self.metadata.assert_job_lease(job)
        current = self.metadata.document(document.document_id)
        if current is None or current.deleted_at is not None:
            raise DocumentNotFoundError("Build source is unavailable")
        return snapshot
