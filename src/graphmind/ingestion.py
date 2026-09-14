"""Deterministic multi-format ingestion and staged dual-store publication."""

from __future__ import annotations

import hashlib
import os
import re
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import Settings
from .domain import Chunk, Document, Job, JobStatus, Version, VersionStatus
from .errors import (
    DocumentBusyError,
    DocumentNotFoundError,
    EmptyDocumentError,
    GraphMindError,
    PathValidationError,
    PublicationError,
    UnsupportedFileError,
)
from .files import FileStore
from .metadata import MetadataStore, utc_now
from .parsing import ExtractionResult, IsolatedExtractor, SUPPORTED_MEDIA_TYPES
from .storage import GraphStore, VectorStore


REFERENCE_PATTERN = re.compile(r"\[\[([^\[\]\r\n]{1,200})\]\]")


def normalize_name(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _within(path: Path, roots: tuple[Path, ...]) -> bool:
    for root in roots:
        try:
            path.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


def deterministic_chunks(
    text: str,
    *,
    document_id: str,
    version_id: str,
    chunk_size: int,
    overlap: int,
) -> list[Chunk]:
    """Preserve the P2 TXT chunking and identifiers exactly."""
    chunks: list[Chunk] = []
    start = 0
    ordinal = 0
    while start < len(text):
        end = min(len(text), start + chunk_size)
        if end < len(text):
            newline = text.rfind("\n", start + max(1, chunk_size // 2), end)
            if newline > start:
                end = newline + 1
        payload = text[start:end]
        if payload.strip():
            line_start = text.count("\n", 0, start) + 1
            line_end = text.count("\n", 0, max(start, end - 1)) + 1
            locator = f"lines {line_start}-{line_end}" if line_start != line_end else f"line {line_start}"
            references = tuple(
                sorted({normalize_name(match) for match in REFERENCE_PATTERN.findall(payload)})
            )
            chunk_material = (
                f"{version_id}\0{ordinal}\0{start}\0{end}\0{payload}".encode("utf-8")
            )
            chunks.append(
                Chunk(
                    chunk_id=_hash(chunk_material),
                    version_id=version_id,
                    document_id=document_id,
                    ordinal=ordinal,
                    text=payload,
                    locator=locator,
                    start_offset=start,
                    end_offset=end,
                    reference_names=references,
                )
            )
            ordinal += 1
        if end >= len(text):
            break
        start = max(start + 1, end - overlap)
    return chunks


def deterministic_segment_chunks(
    extraction: ExtractionResult,
    *,
    document_id: str,
    version_id: str,
    chunk_size: int,
    overlap: int,
) -> list[Chunk]:
    """Chunk within parser segments so every result retains a source locator."""
    chunks: list[Chunk] = []
    ordinal = 0
    absolute_offset = 0
    for segment in extraction.segments:
        text = segment.text
        start = 0
        part = 1
        while start < len(text):
            end = min(len(text), start + chunk_size)
            if end < len(text):
                newline = text.rfind("\n", start + max(1, chunk_size // 2), end)
                if newline > start:
                    end = newline + 1
            payload = text[start:end]
            if payload.strip():
                locator = segment.locator
                if len(text) > chunk_size:
                    locator = f"{locator}, part {part}"
                references = tuple(
                    sorted({normalize_name(match) for match in REFERENCE_PATTERN.findall(payload)})
                )
                global_start = absolute_offset + start
                global_end = absolute_offset + end
                material = (
                    f"{version_id}\0{ordinal}\0{global_start}\0{global_end}\0{locator}\0{payload}"
                ).encode("utf-8")
                chunks.append(
                    Chunk(
                        chunk_id=_hash(material),
                        version_id=version_id,
                        document_id=document_id,
                        ordinal=ordinal,
                        text=payload,
                        locator=locator,
                        start_offset=global_start,
                        end_offset=global_end,
                        reference_names=references,
                    )
                )
                ordinal += 1
                part += 1
            if end >= len(text):
                break
            start = max(start + 1, end - overlap)
        absolute_offset += len(text) + 1
    return chunks


@dataclass(frozen=True, slots=True)
class ImportSubmission:
    document: Document
    version: Version
    job: Job
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DeleteSubmission:
    document: Document
    job: Job


class IngestionService:
    def __init__(
        self,
        settings: Settings,
        metadata: MetadataStore,
        vector: VectorStore,
        graph: GraphStore,
        installation_id: str,
        embedding_fingerprint: str,
        files: FileStore,
        extractor: IsolatedExtractor,
        *,
        failure_injector: Callable[[str], None] | None = None,
    ) -> None:
        self.settings = settings
        self.metadata = metadata
        self.vector = vector
        self.graph = graph
        self.installation_id = installation_id
        self.embedding_fingerprint = embedding_fingerprint
        self.files = files
        self.extractor = extractor
        self.failure_injector = failure_injector

    @property
    def txt_chunker_fingerprint(self) -> str:
        return f"txt-window-v1:size={self.settings.chunk_size}:overlap={self.settings.chunk_overlap}"

    @property
    def segment_chunker_fingerprint(self) -> str:
        return f"segment-window-v1:size={self.settings.chunk_size}:overlap={self.settings.chunk_overlap}"

    @property
    def chunker_fingerprint(self) -> str:
        """Compatibility alias retained for the P2 TXT contract."""
        return self.txt_chunker_fingerprint

    def _fail(self, point: str) -> None:
        if self.failure_injector:
            self.failure_injector(point)

    def _read_source(self, source: Path) -> tuple[Path, bytes, str]:
        try:
            resolved = source.expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise PathValidationError(f"Input file cannot be resolved: {source}") from exc
        if source.is_symlink() or not resolved.is_file():
            raise PathValidationError("Input must be a regular, non-symlink file")
        if not _within(resolved, self.settings.allowed_import_roots):
            raise PathValidationError("Input file is outside the configured import roots")
        suffix = resolved.suffix.casefold()
        if suffix not in SUPPORTED_MEDIA_TYPES:
            raise UnsupportedFileError("Supported formats are PDF, DOCX, TXT, Markdown, and CSV")
        try:
            raw = resolved.read_bytes()
        except OSError as exc:
            raise PathValidationError("Input file is unreadable") from exc
        return resolved, raw, suffix

    def prepare_txt_import(self, source: Path) -> ImportSubmission:
        if source.suffix.casefold() != ".txt":
            raise UnsupportedFileError("This compatibility entry point accepts TXT files only")
        return self.prepare_import(source)

    def prepare_import(self, source: Path) -> ImportSubmission:
        resolved, raw, suffix = self._read_source(source)
        extraction = self.extractor.extract(raw, suffix)

        source_key = _hash(os.path.normcase(str(resolved)).encode("utf-8"))
        document_id = str(uuid.uuid5(uuid.UUID(self.installation_id), f"document:{source_key}"))
        existing = self.metadata.document_by_source_key(source_key)
        allow_restore = False
        if existing is not None and existing.deleted_at is not None:
            cleanup_jobs = [
                job
                for job in self.metadata.list_jobs()
                if job.operation == "delete_document" and job.document_id == document_id
            ]
            if not cleanup_jobs or cleanup_jobs[-1].status is not JobStatus.SUCCEEDED:
                raise DocumentBusyError(
                    "Document deletion cleanup must succeed before it can be imported again"
                )
            allow_restore = True
        created_at = existing.created_at if existing else utc_now()
        document = Document(
            document_id=document_id,
            collection_id=self.settings.collection_id,
            display_name=resolved.name,
            normalized_name=normalize_name(resolved.name),
            media_type=extraction.media_type,
            source_key=source_key,
            created_at=created_at,
            active_version_id=existing.active_version_id if existing else None,
        )
        canonical_text = "\n".join(segment.text for segment in extraction.segments)
        if not canonical_text.strip():
            raise EmptyDocumentError("Input produced no indexable text")
        content_hash = _hash(canonical_text.encode("utf-8"))
        chunker_fingerprint = (
            self.txt_chunker_fingerprint if suffix == ".txt" else self.segment_chunker_fingerprint
        )
        version_material = (
            f"{content_hash}\0{extraction.extractor_fingerprint}\0{chunker_fingerprint}"
            f"\0{self.embedding_fingerprint}"
        )
        version_id = str(uuid.uuid5(uuid.UUID(document_id), version_material))
        if suffix == ".txt":
            chunks = deterministic_chunks(
                canonical_text,
                document_id=document_id,
                version_id=version_id,
                chunk_size=self.settings.chunk_size,
                overlap=self.settings.chunk_overlap,
            )
        else:
            chunks = deterministic_segment_chunks(
                extraction,
                document_id=document_id,
                version_id=version_id,
                chunk_size=self.settings.chunk_size,
                overlap=self.settings.chunk_overlap,
            )
        if not chunks:
            raise EmptyDocumentError("Input produced no indexable chunks")
        chunk_digest = _hash("\n".join(chunk.chunk_id for chunk in chunks).encode("ascii"))
        private_ref = self.files.private_ref(document_id, version_id, suffix)
        version = Version(
            version_id=version_id,
            document_id=document_id,
            content_hash=content_hash,
            extractor_fingerprint=extraction.extractor_fingerprint,
            chunker_fingerprint=chunker_fingerprint,
            embedding_fingerprint=self.embedding_fingerprint,
            private_file_ref=private_ref,
            source_size=len(raw),
            expected_chunk_count=len(chunks),
            chunk_digest=chunk_digest,
            status=VersionStatus.STAGED,
            created_at=utc_now(),
        )
        idempotency_key = f"import:{version_id}"
        job_id = str(uuid.uuid5(uuid.UUID(self.installation_id), idempotency_key))
        stored_version = self.metadata.version(version_id)
        stored_job = self.metadata.job(job_id)
        if (
            stored_version is not None
            and stored_version.status is VersionStatus.READY
            and existing is not None
            and existing.deleted_at is None
            and existing.active_version_id == version_id
            and stored_job is not None
            and stored_job.status is JobStatus.SUCCEEDED
        ):
            return ImportSubmission(existing, stored_version, stored_job, extraction.warnings)

        self.files.put_bytes(document_id, version_id, raw, suffix)
        self._fail("after_stage")
        stored_document = self.metadata.put_document(document, allow_restore=allow_restore)
        if stored_document.deleted_at is not None:
            self.files.delete_document(document_id)
            raise DocumentBusyError("Document was deleted while the import was being prepared")
        document = stored_document
        self.metadata.put_version(version)
        self.metadata.replace_chunks(version_id, chunks)
        self._fail("after_metadata")
        job = self.metadata.put_job(
            Job(
                job_id=job_id,
                idempotency_key=idempotency_key,
                operation="import_document",
                document_id=document_id,
                version_id=version_id,
                status=JobStatus.QUEUED,
                attempt_count=0,
                available_at=utc_now(),
            ),
            max_queued_jobs=self.settings.max_queued_jobs,
        )
        if job.status is JobStatus.SUCCEEDED and (existing is None or existing.active_version_id != version_id):
            self.metadata.retry_job(job.job_id, include_succeeded=True)
            job = self.metadata.job(job.job_id) or job
        return ImportSubmission(document, version, job, extraction.warnings)

    def prepare_delete(self, document_id: str) -> DeleteSubmission:
        document = self.metadata.document(document_id)
        if document is None:
            raise DocumentNotFoundError("Document does not exist")
        if document.deleted_at is not None:
            jobs = [
                job
                for job in self.metadata.list_jobs()
                if job.operation == "delete_document" and job.document_id == document_id
            ]
            if not jobs:
                raise PublicationError("Deleted document has no cleanup job")
            return DeleteSubmission(document, jobs[-1])
        nonce = uuid.uuid4().hex
        idempotency_key = f"delete:{document_id}:{document.active_version_id or 'none'}:{nonce}"
        job = Job(
            job_id=str(uuid.uuid5(uuid.UUID(self.installation_id), idempotency_key)),
            idempotency_key=idempotency_key,
            operation="delete_document",
            document_id=document_id,
            version_id=document.active_version_id or "none",
            status=JobStatus.QUEUED,
            attempt_count=0,
            available_at=utc_now(),
        )
        tombstoned, stored_job = self.metadata.tombstone_and_put_job(
            document_id,
            document.active_version_id,
            job,
            max_queued_jobs=self.settings.max_queued_jobs,
        )
        return DeleteSubmission(tombstoned, stored_job)

    def _progress(self, job: Job, value: int) -> None:
        if job.lease_owner:
            self.metadata.update_job_progress(job.job_id, job.lease_owner, value)

    def _process_import(self, job: Job) -> None:
        document = self.metadata.document(job.document_id)
        version = self.metadata.version(job.version_id)
        if document is None or version is None:
            raise PublicationError("Job metadata is incomplete")
        chunks = self.metadata.all_chunks_for_version(version.version_id)
        expected_ids = {chunk.chunk_id for chunk in chunks}
        chunk_digest = _hash("\n".join(chunk.chunk_id for chunk in chunks).encode("ascii"))
        if (
            len(chunks) != version.expected_chunk_count
            or len(expected_ids) != version.expected_chunk_count
            or chunk_digest != version.chunk_digest
        ):
            raise PublicationError("Staged chunk count does not match the version manifest")
        if document.active_version_id == version.version_id and version.status is VersionStatus.READY:
            stored_ids = (
                expected_ids,
                self.vector.chunk_ids_for_version(self.installation_id, version.version_id),
                self.graph.chunk_ids_for_version(self.installation_id, version.version_id),
            )
            if all(item == expected_ids for item in stored_ids):
                self._progress(job, 99)
                return
            raise PublicationError("The active version does not match both retrieval stores")
        try:
            self.metadata.set_version_status(version.version_id, VersionStatus.INDEXING)
            self._progress(job, 10)
            self.vector.upsert_version(self.installation_id, chunks)
            self._progress(job, 40)
            self._fail("after_vector")
            self.graph.upsert_version(self.installation_id, document, version, chunks)
            self._progress(job, 70)
            self._fail("after_graph")
            stored_ids = (
                expected_ids,
                self.vector.chunk_ids_for_version(self.installation_id, version.version_id),
                self.graph.chunk_ids_for_version(self.installation_id, version.version_id),
            )
            if any(item != expected_ids for item in stored_ids):
                raise PublicationError(
                    "Dual-store verification failed: stored chunk IDs do not match the manifest"
                )
            self._progress(job, 90)
            self._fail("before_publish")
            self.metadata.publish_version(version.version_id)
            self._progress(job, 99)
            self._fail("after_publish")
        except Exception as exc:
            current = self.metadata.document(job.document_id)
            if current is None or current.active_version_id != job.version_id:
                code = exc.code if isinstance(exc, GraphMindError) else "internal_error"
                self.metadata.set_version_status(job.version_id, VersionStatus.FAILED, code)
            raise

    def _process_delete(self, job: Job) -> None:
        self.vector.delete_document(self.installation_id, job.document_id)
        self._progress(job, 35)
        self._fail("after_delete_vector")
        self.graph.delete_document(self.installation_id, job.document_id)
        self._progress(job, 70)
        self._fail("after_delete_graph")
        self.files.delete_document(job.document_id)
        self._progress(job, 99)
        self._fail("after_delete_files")

    def process_job(self, job: Job) -> None:
        if job.operation in {"import_txt", "import_document"}:
            self._process_import(job)
            return
        if job.operation == "delete_document":
            self._process_delete(job)
            return
        raise PublicationError(f"Unsupported job operation: {job.operation}")
