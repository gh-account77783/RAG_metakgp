"""SQLite metadata, manifest, migration, and durable-job persistence."""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterator, Sequence

from .domain import Chunk, Document, Job, JobStatus, Version, VersionStatus
from .errors import (
    DocumentBusyError,
    DocumentNotFoundError,
    JobQueueFullError,
    ManifestUnavailableError,
    MigrationBusyError,
    MigrationError,
    UnsupportedSchemaError,
    RetryableQueryError,
)


CURRENT_SCHEMA_VERSION = 2


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def utc_after(seconds: int) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(timespec="microseconds")


_MIGRATION_1 = """
CREATE TABLE IF NOT EXISTS installation_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    installation_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
    document_id TEXT PRIMARY KEY,
    collection_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    media_type TEXT NOT NULL,
    source_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    active_version_id TEXT,
    deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_documents_normalized_name
    ON documents(collection_id, normalized_name);
CREATE TABLE IF NOT EXISTS versions (
    version_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(document_id),
    content_hash TEXT NOT NULL,
    extractor_fingerprint TEXT NOT NULL,
    chunker_fingerprint TEXT NOT NULL,
    embedding_fingerprint TEXT NOT NULL,
    private_file_ref TEXT NOT NULL,
    expected_chunk_count INTEGER NOT NULL,
    chunk_digest TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('staged', 'indexing', 'ready', 'failed')),
    created_at TEXT NOT NULL,
    failure_code TEXT,
    UNIQUE(document_id, content_hash, extractor_fingerprint, chunker_fingerprint, embedding_fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_versions_document ON versions(document_id);
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES versions(version_id) ON DELETE CASCADE,
    document_id TEXT NOT NULL REFERENCES documents(document_id),
    ordinal INTEGER NOT NULL,
    text TEXT NOT NULL,
    locator TEXT NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    reference_names TEXT NOT NULL DEFAULT '[]',
    UNIQUE(version_id, ordinal)
);
CREATE INDEX IF NOT EXISTS idx_chunks_version ON chunks(version_id);
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    operation TEXT NOT NULL,
    document_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL,
    lease_owner TEXT,
    lease_expires_at TEXT,
    heartbeat_at TEXT,
    progress INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_claim
    ON jobs(status, available_at, created_at);
CREATE TABLE IF NOT EXISTS writer_lock (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    owner TEXT,
    lease_expires_at TEXT
);
INSERT OR IGNORE INTO writer_lock(singleton, owner, lease_expires_at)
VALUES (1, NULL, NULL);
"""

_MIGRATION_2 = "ALTER TABLE versions ADD COLUMN source_size INTEGER NOT NULL DEFAULT 0;"


def _execute_script(connection: sqlite3.Connection, script: str) -> None:
    for statement in script.split(";"):
        if statement.strip():
            connection.execute(statement)


class MetadataStore:
    def __init__(self, path: Path, *, busy_timeout_ms: int = 500) -> None:
        self.path = path
        self.busy_timeout_ms = busy_timeout_ms

    def _connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(
                self.path,
                timeout=self.busy_timeout_ms / 1000,
                isolation_level=None,
            )
        except sqlite3.Error as exc:
            raise ManifestUnavailableError(f"Cannot open metadata store: {self.path}") from exc
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self, mode: str = "IMMEDIATE") -> Iterator[sqlite3.Connection]:
        with self.connection() as connection:
            try:
                connection.execute(f"BEGIN {mode}")
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def migrate(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.connection() as connection:
                connection.execute("BEGIN EXCLUSIVE")
                current = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if current > CURRENT_SCHEMA_VERSION:
                    raise UnsupportedSchemaError(
                        f"Metadata schema {current} is newer than supported schema {CURRENT_SCHEMA_VERSION}"
                    )
                if current < 1:
                    _execute_script(connection, _MIGRATION_1)
                    connection.execute("PRAGMA user_version = 1")
                    current = 1
                if current < 2:
                    connection.execute(_MIGRATION_2)
                    connection.execute("PRAGMA user_version = 2")
                connection.commit()
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise MigrationBusyError("Another process holds the metadata migration lock") from exc
            raise MigrationError("Metadata migration failed") from exc
        except sqlite3.Error as exc:
            raise MigrationError("Metadata migration failed") from exc

    def schema_version(self) -> int:
        with self.connection() as connection:
            return int(connection.execute("PRAGMA user_version").fetchone()[0])

    def installation_id(self) -> str:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT installation_id FROM installation_state WHERE singleton = 1"
            ).fetchone()
            if row:
                return str(row["installation_id"])
            value = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO installation_state(singleton, installation_id, created_at) VALUES (1, ?, ?)",
                (value, utc_now()),
            )
            return value

    def put_document(self, document: Document, *, allow_restore: bool = False) -> Document:
        with self.transaction() as connection:
            current = connection.execute(
                "SELECT * FROM documents WHERE document_id = ?", (document.document_id,)
            ).fetchone()
            if current is not None and current["deleted_at"] is not None and allow_restore:
                cleanup = connection.execute(
                    """
                    SELECT status FROM jobs
                    WHERE operation = 'delete_document' AND document_id = ?
                    ORDER BY created_at DESC, job_id DESC LIMIT 1
                    """,
                    (document.document_id,),
                ).fetchone()
                if cleanup is None or cleanup["status"] != JobStatus.SUCCEEDED.value:
                    raise DocumentBusyError(
                        "Document deletion cleanup must succeed before it can be imported again"
                    )
            connection.execute(
                """
                INSERT INTO documents(
                    document_id, collection_id, display_name, normalized_name, media_type,
                    source_key, created_at, active_version_id, deleted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(document_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    normalized_name = excluded.normalized_name,
                    media_type = excluded.media_type,
                    deleted_at = CASE WHEN ? = 1 THEN NULL ELSE documents.deleted_at END
                """,
                (
                    document.document_id,
                    document.collection_id,
                    document.display_name,
                    document.normalized_name,
                    document.media_type,
                    document.source_key,
                    document.created_at,
                    document.active_version_id,
                    document.deleted_at,
                    1 if allow_restore else 0,
                ),
            )
            stored = connection.execute(
                "SELECT * FROM documents WHERE document_id = ?", (document.document_id,)
            ).fetchone()
        return self._document(stored)

    def document_by_source_key(self, source_key: str) -> Document | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM documents WHERE source_key = ?", (source_key,)
            ).fetchone()
        return self._document(row) if row else None

    def document(self, document_id: str) -> Document | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM documents WHERE document_id = ?", (document_id,)
            ).fetchone()
        return self._document(row) if row else None

    def list_documents(self) -> list[Document]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM documents ORDER BY created_at, document_id"
            ).fetchall()
        return [self._document(row) for row in rows]

    def put_version(self, version: Version) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO versions(
                    version_id, document_id, content_hash, extractor_fingerprint,
                    chunker_fingerprint, embedding_fingerprint, private_file_ref,
                    expected_chunk_count, chunk_digest, status, created_at, failure_code, source_size
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(version_id) DO UPDATE SET
                    private_file_ref = excluded.private_file_ref,
                    expected_chunk_count = excluded.expected_chunk_count,
                    chunk_digest = excluded.chunk_digest,
                    source_size = excluded.source_size
                """,
                (
                    version.version_id,
                    version.document_id,
                    version.content_hash,
                    version.extractor_fingerprint,
                    version.chunker_fingerprint,
                    version.embedding_fingerprint,
                    version.private_file_ref,
                    version.expected_chunk_count,
                    version.chunk_digest,
                    version.status.value,
                    version.created_at,
                    version.failure_code,
                    version.source_size,
                ),
            )

    def version(self, version_id: str) -> Version | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM versions WHERE version_id = ?", (version_id,)
            ).fetchone()
        return self._version(row) if row else None

    def set_version_status(
        self, version_id: str, status: VersionStatus, failure_code: str | None = None
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE versions SET status = ?, failure_code = ? WHERE version_id = ?",
                (status.value, failure_code, version_id),
            )

    def replace_chunks(self, version_id: str, chunks: Sequence[Chunk]) -> None:
        import json

        with self.transaction() as connection:
            connection.execute("DELETE FROM chunks WHERE version_id = ?", (version_id,))
            connection.executemany(
                """
                INSERT INTO chunks(
                    chunk_id, version_id, document_id, ordinal, text, locator,
                    start_offset, end_offset, reference_names
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        chunk.chunk_id,
                        chunk.version_id,
                        chunk.document_id,
                        chunk.ordinal,
                        chunk.text,
                        chunk.locator,
                        chunk.start_offset,
                        chunk.end_offset,
                        json.dumps(chunk.reference_names),
                    )
                    for chunk in chunks
                ],
            )

    def chunk_count(self, version_id: str) -> int:
        with self.connection() as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM chunks WHERE version_id = ?", (version_id,)
                ).fetchone()[0]
            )

    def publish_version(self, version_id: str) -> None:
        with self.transaction() as connection:
            row = connection.execute(
                """
                SELECT v.document_id, v.expected_chunk_count, d.deleted_at
                FROM versions v JOIN documents d ON d.document_id = v.document_id
                WHERE v.version_id = ?
                """,
                (version_id,),
            ).fetchone()
            if row is None:
                raise ManifestUnavailableError("Cannot publish an unknown version")
            if row["deleted_at"] is not None:
                raise ManifestUnavailableError("Cannot publish a tombstoned document")
            actual = int(
                connection.execute(
                    "SELECT COUNT(*) FROM chunks WHERE version_id = ?", (version_id,)
                ).fetchone()[0]
            )
            if actual != int(row["expected_chunk_count"]):
                raise ManifestUnavailableError(
                    f"Version chunk count mismatch: expected {row['expected_chunk_count']}, found {actual}"
                )
            connection.execute(
                "UPDATE versions SET status = 'ready', failure_code = NULL WHERE version_id = ?",
                (version_id,),
            )
            connection.execute(
                "UPDATE documents SET active_version_id = ?, deleted_at = NULL WHERE document_id = ?",
                (version_id, row["document_id"]),
            )

    def active_version_ids(self) -> set[str]:
        try:
            with self.connection() as connection:
                rows = connection.execute(
                    """
                    SELECT d.active_version_id
                    FROM documents d
                    JOIN versions v ON v.version_id = d.active_version_id
                    WHERE d.deleted_at IS NULL AND v.status = 'ready'
                    """
                ).fetchall()
        except sqlite3.Error as exc:
            raise ManifestUnavailableError("Cannot read the active document manifest") from exc
        return {str(row[0]) for row in rows}

    def active_embedding_fingerprints(self) -> set[str]:
        try:
            with self.connection() as connection:
                rows = connection.execute(
                    """
                    SELECT DISTINCT v.embedding_fingerprint
                    FROM documents d
                    JOIN versions v ON v.version_id = d.active_version_id
                    WHERE d.deleted_at IS NULL AND v.status = 'ready'
                    """
                ).fetchall()
        except sqlite3.Error as exc:
            raise ManifestUnavailableError("Cannot read active embedding fingerprints") from exc
        return {str(row[0]) for row in rows}

    def active_chunks(self, chunk_ids: Sequence[str]) -> dict[str, tuple[Chunk, Document]]:
        if not chunk_ids:
            return {}
        placeholders = ",".join("?" for _ in chunk_ids)
        query = f"""
            SELECT c.*, d.collection_id, d.display_name, d.normalized_name, d.media_type,
                   d.source_key, d.created_at AS document_created_at,
                   d.active_version_id, d.deleted_at
            FROM chunks c
            JOIN documents d ON d.document_id = c.document_id
            JOIN versions v ON v.version_id = c.version_id
            WHERE c.chunk_id IN ({placeholders})
              AND d.active_version_id = c.version_id
              AND d.deleted_at IS NULL
              AND v.status = 'ready'
        """
        try:
            with self.connection() as connection:
                rows = connection.execute(query, tuple(chunk_ids)).fetchall()
        except sqlite3.Error as exc:
            raise ManifestUnavailableError("Cannot resolve active evidence") from exc
        result: dict[str, tuple[Chunk, Document]] = {}
        for row in rows:
            chunk = self._chunk(row)
            document = Document(
                document_id=str(row["document_id"]),
                collection_id=str(row["collection_id"]),
                display_name=str(row["display_name"]),
                normalized_name=str(row["normalized_name"]),
                media_type=str(row["media_type"]),
                source_key=str(row["source_key"]),
                created_at=str(row["document_created_at"]),
                active_version_id=str(row["active_version_id"]),
                deleted_at=row["deleted_at"],
            )
            result[chunk.chunk_id] = (chunk, document)
        return result

    def all_chunks_for_version(self, version_id: str) -> list[Chunk]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM chunks WHERE version_id = ? ORDER BY ordinal", (version_id,)
            ).fetchall()
        return [self._chunk(row) for row in rows]

    def put_job(self, job: Job, *, max_queued_jobs: int | None = None) -> Job:
        now = utc_now()
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM jobs WHERE idempotency_key = ?", (job.idempotency_key,)
            ).fetchone()
            if existing is not None:
                return self._job(existing)
            if max_queued_jobs is not None:
                queued = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM jobs WHERE status IN ('queued', 'running')"
                    ).fetchone()[0]
                )
                if queued >= max_queued_jobs:
                    raise JobQueueFullError(
                        f"Ingestion queue limit of {max_queued_jobs} has been reached"
                    )
            connection.execute(
                """
                INSERT INTO jobs(
                    job_id, idempotency_key, operation, document_id, version_id, status,
                    attempt_count, available_at, lease_owner, lease_expires_at, heartbeat_at,
                    progress, error_code, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.job_id,
                    job.idempotency_key,
                    job.operation,
                    job.document_id,
                    job.version_id,
                    job.status.value,
                    job.attempt_count,
                    job.available_at,
                    job.lease_owner,
                    job.lease_expires_at,
                    job.heartbeat_at,
                    job.progress,
                    job.error_code,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM jobs WHERE idempotency_key = ?", (job.idempotency_key,)
            ).fetchone()
        return self._job(row)

    def tombstone_and_put_job(
        self,
        document_id: str,
        expected_active_version_id: str | None,
        job: Job,
        *,
        max_queued_jobs: int,
    ) -> tuple[Document, Job]:
        now = utc_now()
        with self.transaction() as connection:
            document_row = connection.execute(
                "SELECT * FROM documents WHERE document_id = ?", (document_id,)
            ).fetchone()
            if document_row is None:
                raise DocumentNotFoundError("Document does not exist")
            if document_row["deleted_at"] is not None:
                existing = connection.execute(
                    """
                    SELECT * FROM jobs
                    WHERE operation = 'delete_document' AND document_id = ?
                    ORDER BY created_at DESC, job_id DESC LIMIT 1
                    """,
                    (document_id,),
                ).fetchone()
                if existing is None:
                    raise ManifestUnavailableError(
                        "Deleted document has no cleanup job"
                    )
                return self._document(document_row), self._job(existing)
            if document_row["active_version_id"] != expected_active_version_id:
                raise RetryableQueryError("Document version changed while deletion was prepared")
            existing = connection.execute(
                "SELECT * FROM jobs WHERE idempotency_key = ?", (job.idempotency_key,)
            ).fetchone()
            if existing is not None:
                connection.execute(
                    "UPDATE documents SET active_version_id = NULL, deleted_at = ? WHERE document_id = ?",
                    (now, document_id),
                )
                updated_document = connection.execute(
                    "SELECT * FROM documents WHERE document_id = ?", (document_id,)
                ).fetchone()
                return self._document(updated_document), self._job(existing)
            connection.execute(
                """
                UPDATE jobs SET status = 'failed', error_code = 'superseded_by_delete',
                    lease_owner = NULL, lease_expires_at = NULL, heartbeat_at = NULL,
                    updated_at = ?
                WHERE document_id = ? AND operation IN ('import_txt', 'import_document')
                  AND status = 'queued'
                """,
                (now, document_id),
            )
            queued = int(
                connection.execute(
                    "SELECT COUNT(*) FROM jobs WHERE status IN ('queued', 'running')"
                ).fetchone()[0]
            )
            if queued >= max_queued_jobs:
                raise JobQueueFullError(
                    f"Ingestion queue limit of {max_queued_jobs} has been reached"
                )
            connection.execute(
                "UPDATE documents SET active_version_id = NULL, deleted_at = ? WHERE document_id = ?",
                (now, document_id),
            )
            connection.execute(
                """
                INSERT INTO jobs(
                    job_id, idempotency_key, operation, document_id, version_id, status,
                    attempt_count, available_at, lease_owner, lease_expires_at, heartbeat_at,
                    progress, error_code, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.job_id,
                    job.idempotency_key,
                    job.operation,
                    job.document_id,
                    job.version_id,
                    job.status.value,
                    job.attempt_count,
                    job.available_at,
                    job.lease_owner,
                    job.lease_expires_at,
                    job.heartbeat_at,
                    job.progress,
                    job.error_code,
                    now,
                    now,
                ),
            )
            updated_document = connection.execute(
                "SELECT * FROM documents WHERE document_id = ?", (document_id,)
            ).fetchone()
            stored_job = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job.job_id,)
            ).fetchone()
        return self._document(updated_document), self._job(stored_job)

    def retry_job(self, job_id: str, *, include_succeeded: bool = False) -> None:
        now = utc_now()
        eligible = "('failed', 'succeeded')" if include_succeeded else "('failed')"
        with self.transaction() as connection:
            connection.execute(
                f"""
                UPDATE jobs SET status = 'queued', available_at = ?, lease_owner = NULL,
                    lease_expires_at = NULL, heartbeat_at = NULL, error_code = NULL,
                    progress = 0, updated_at = ?
                WHERE job_id = ? AND status IN {eligible}
                """,
                (now, now, job_id),
            )

    def update_job_progress(self, job_id: str, owner: str, progress: int) -> bool:
        bounded = max(0, min(99, int(progress)))
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs SET progress = ?, updated_at = ?
                WHERE job_id = ? AND status = 'running' AND lease_owner = ?
                """,
                (bounded, utc_now(), job_id, owner),
            )
            return cursor.rowcount == 1

    def acquire_writer(self, owner: str, lease_seconds: int) -> bool:
        now = utc_now()
        expires = utc_after(lease_seconds)
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE writer_lock SET owner = ?, lease_expires_at = ?
                WHERE singleton = 1
                  AND (owner IS NULL OR owner = ? OR lease_expires_at < ?)
                """,
                (owner, expires, owner, now),
            )
            return cursor.rowcount == 1

    def release_writer(self, owner: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE writer_lock SET owner = NULL, lease_expires_at = NULL WHERE singleton = 1 AND owner = ?",
                (owner,),
            )

    def heartbeat(self, job_id: str, owner: str, lease_seconds: int) -> bool:
        now = utc_now()
        expires = utc_after(lease_seconds)
        with self.transaction() as connection:
            writer = connection.execute(
                """
                UPDATE writer_lock SET lease_expires_at = ?
                WHERE singleton = 1 AND owner = ?
                """,
                (expires, owner),
            )
            job = connection.execute(
                """
                UPDATE jobs SET lease_expires_at = ?, heartbeat_at = ?, updated_at = ?
                WHERE job_id = ? AND status = 'running' AND lease_owner = ?
                """,
                (expires, now, now, job_id, owner),
            )
            return writer.rowcount == 1 and job.rowcount == 1

    def claim_next_job(self, owner: str, lease_seconds: int) -> Job | None:
        now = utc_now()
        expires = utc_after(lease_seconds)
        with self.transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE status = 'queued' AND available_at <= ?
                ORDER BY created_at, job_id LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE jobs SET status = 'running', attempt_count = attempt_count + 1,
                    lease_owner = ?, lease_expires_at = ?, heartbeat_at = ?, updated_at = ?
                WHERE job_id = ? AND status = 'queued'
                """,
                (owner, expires, now, now, row["job_id"]),
            )
            updated = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (row["job_id"],)
            ).fetchone()
        return self._job(updated)

    def finish_job(self, job_id: str, owner: str, *, succeeded: bool, error_code: str | None = None) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE jobs SET status = ?,
                    progress = CASE WHEN ? = 1 THEN 100 ELSE progress END,
                    error_code = ?, lease_owner = NULL,
                    lease_expires_at = NULL, heartbeat_at = NULL, updated_at = ?
                WHERE job_id = ? AND status = 'running' AND lease_owner = ?
                """,
                (
                    JobStatus.SUCCEEDED.value if succeeded else JobStatus.FAILED.value,
                    1 if succeeded else 0,
                    error_code,
                    utc_now(),
                    job_id,
                    owner,
                ),
            )

    def reconcile(self) -> int:
        now = utc_now()
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs SET status = 'queued', lease_owner = NULL, lease_expires_at = NULL,
                    heartbeat_at = NULL, error_code = 'interrupted', updated_at = ?
                WHERE status = 'running' AND lease_expires_at < ?
                """,
                (now, now),
            )
            connection.execute(
                "UPDATE writer_lock SET owner = NULL, lease_expires_at = NULL WHERE lease_expires_at < ?",
                (now,),
            )
            return cursor.rowcount

    def list_jobs(self) -> list[Job]:
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM jobs ORDER BY created_at, job_id").fetchall()
        return [self._job(row) for row in rows]

    def job(self, job_id: str) -> Job | None:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return self._job(row) if row else None

    @staticmethod
    def _document(row: sqlite3.Row) -> Document:
        return Document(
            document_id=str(row["document_id"]),
            collection_id=str(row["collection_id"]),
            display_name=str(row["display_name"]),
            normalized_name=str(row["normalized_name"]),
            media_type=str(row["media_type"]),
            source_key=str(row["source_key"]),
            created_at=str(row["created_at"]),
            active_version_id=row["active_version_id"],
            deleted_at=row["deleted_at"],
        )

    @staticmethod
    def _version(row: sqlite3.Row) -> Version:
        return Version(
            version_id=str(row["version_id"]),
            document_id=str(row["document_id"]),
            content_hash=str(row["content_hash"]),
            extractor_fingerprint=str(row["extractor_fingerprint"]),
            chunker_fingerprint=str(row["chunker_fingerprint"]),
            embedding_fingerprint=str(row["embedding_fingerprint"]),
            private_file_ref=str(row["private_file_ref"]),
            source_size=int(row["source_size"]),
            expected_chunk_count=int(row["expected_chunk_count"]),
            chunk_digest=str(row["chunk_digest"]),
            status=VersionStatus(str(row["status"])),
            created_at=str(row["created_at"]),
            failure_code=row["failure_code"],
        )

    @staticmethod
    def _chunk(row: sqlite3.Row) -> Chunk:
        import json

        return Chunk(
            chunk_id=str(row["chunk_id"]),
            version_id=str(row["version_id"]),
            document_id=str(row["document_id"]),
            ordinal=int(row["ordinal"]),
            text=str(row["text"]),
            locator=str(row["locator"]),
            start_offset=int(row["start_offset"]),
            end_offset=int(row["end_offset"]),
            reference_names=tuple(json.loads(str(row["reference_names"]))),
        )

    @staticmethod
    def _job(row: sqlite3.Row) -> Job:
        return Job(
            job_id=str(row["job_id"]),
            idempotency_key=str(row["idempotency_key"]),
            operation=str(row["operation"]),
            document_id=str(row["document_id"]),
            version_id=str(row["version_id"]),
            status=JobStatus(str(row["status"])),
            attempt_count=int(row["attempt_count"]),
            available_at=str(row["available_at"]),
            lease_owner=row["lease_owner"],
            lease_expires_at=row["lease_expires_at"],
            heartbeat_at=row["heartbeat_at"],
            progress=int(row["progress"]),
            error_code=row["error_code"],
        )
