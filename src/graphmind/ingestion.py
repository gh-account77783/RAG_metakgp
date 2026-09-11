"""Deterministic TXT ingestion and staged dual-store publication."""

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
    DocumentEncodingError,
    DocumentTooLargeError,
    EmptyDocumentError,
    GraphMindError,
    PathValidationError,
    PublicationError,
    UnsupportedFileError,
)
from .files import FileStore
from .metadata import MetadataStore, utc_now
from .storage import GraphStore, VectorStore


TXT_MEDIA_TYPE = "text/plain"
EXTRACTOR_FINGERPRINT = "txt-utf8-nfc-v1"
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


@dataclass(frozen=True, slots=True)
class ImportSubmission:
    document: Document
    version: Version
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
        self.failure_injector = failure_injector

    @property
    def chunker_fingerprint(self) -> str:
        return f"txt-window-v1:size={self.settings.chunk_size}:overlap={self.settings.chunk_overlap}"

    def _fail(self, point: str) -> None:
        if self.failure_injector:
            self.failure_injector(point)

    def prepare_txt_import(self, source: Path) -> ImportSubmission:
        try:
            resolved = source.expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise PathValidationError(f"Input file cannot be resolved: {source}") from exc
        if source.is_symlink() or not resolved.is_file():
            raise PathValidationError("Input must be a regular, non-symlink file")
        if not _within(resolved, self.settings.allowed_import_roots):
            raise PathValidationError("Input file is outside the configured import roots")
        if resolved.suffix.casefold() != ".txt":
            raise UnsupportedFileError("P2 accepts TXT files only")
        try:
            raw = resolved.read_bytes()
        except OSError as exc:
            raise PathValidationError("Input file is unreadable") from exc
        size = len(raw)
        if size == 0:
            raise EmptyDocumentError("Input file is empty")
        if size > self.settings.max_file_bytes:
            raise DocumentTooLargeError(
                f"Input exceeds the {self.settings.max_file_bytes}-byte limit"
            )
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise DocumentEncodingError("TXT input must use UTF-8 encoding") from exc
        text = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
        if not text.strip():
            raise EmptyDocumentError("Input contains no text")

        source_key = _hash(os.path.normcase(str(resolved)).encode("utf-8"))
        document_id = str(uuid.uuid5(uuid.UUID(self.installation_id), f"document:{source_key}"))
        existing = self.metadata.document_by_source_key(source_key)
        created_at = existing.created_at if existing else utc_now()
        document = Document(
            document_id=document_id,
            collection_id=self.settings.collection_id,
            display_name=resolved.name,
            normalized_name=normalize_name(resolved.name),
            media_type=TXT_MEDIA_TYPE,
            source_key=source_key,
            created_at=created_at,
            active_version_id=existing.active_version_id if existing else None,
        )
        content_hash = _hash(text.encode("utf-8"))
        version_material = (
            f"{content_hash}\0{EXTRACTOR_FINGERPRINT}\0{self.chunker_fingerprint}"
            f"\0{self.embedding_fingerprint}"
        )
        version_id = str(uuid.uuid5(uuid.UUID(document_id), version_material))
        chunks = deterministic_chunks(
            text,
            document_id=document_id,
            version_id=version_id,
            chunk_size=self.settings.chunk_size,
            overlap=self.settings.chunk_overlap,
        )
        if not chunks:
            raise EmptyDocumentError("Input produced no indexable chunks")
        chunk_digest = _hash("\n".join(chunk.chunk_id for chunk in chunks).encode("ascii"))
        private_ref = self.files.private_ref(document_id, version_id)
        version = Version(
            version_id=version_id,
            document_id=document_id,
            content_hash=content_hash,
            extractor_fingerprint=EXTRACTOR_FINGERPRINT,
            chunker_fingerprint=self.chunker_fingerprint,
            embedding_fingerprint=self.embedding_fingerprint,
            private_file_ref=private_ref,
            source_size=size,
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
            and existing.active_version_id == version_id
            and stored_job is not None
            and stored_job.status is JobStatus.SUCCEEDED
        ):
            return ImportSubmission(document=existing, version=stored_version, job=stored_job)

        self.files.put_bytes(document_id, version_id, raw)
        self._fail("after_stage")

        self.metadata.put_document(document)
        self.metadata.put_version(version)
        self.metadata.replace_chunks(version_id, chunks)
        self._fail("after_metadata")

        job = self.metadata.put_job(
            Job(
                job_id=job_id,
                idempotency_key=idempotency_key,
                operation="import_txt",
                document_id=document_id,
                version_id=version_id,
                status=JobStatus.QUEUED,
                attempt_count=0,
                available_at=utc_now(),
            ),
            max_queued_jobs=self.settings.max_queued_jobs,
        )
        return ImportSubmission(document=document, version=version, job=job)

    def process_job(self, job: Job) -> None:
        if job.operation != "import_txt":
            raise PublicationError(f"Unsupported job operation: {job.operation}")
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
                return
            raise PublicationError("The active version does not match both retrieval stores")
        try:
            self.metadata.set_version_status(version.version_id, VersionStatus.INDEXING)
            self.vector.upsert_version(self.installation_id, chunks)
            self._fail("after_vector")
            self.graph.upsert_version(self.installation_id, document, version, chunks)
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
            self._fail("before_publish")
            self.metadata.publish_version(version.version_id)
            self._fail("after_publish")
        except Exception as exc:
            current = self.metadata.document(job.document_id)
            if current is None or current.active_version_id != job.version_id:
                code = exc.code if isinstance(exc, GraphMindError) else "internal_error"
                self.metadata.set_version_status(job.version_id, VersionStatus.FAILED, code)
            raise
