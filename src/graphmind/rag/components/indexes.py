"""Derived-index records in the existing metadata and durable-job authority."""
from __future__ import annotations

import json
import re
import sqlite3
import uuid

from ...errors import JobInputError, ManifestUnavailableError, PublicationError
from ...metadata import MetadataStore, utc_now
from ...sources import SourceService
from .contracts import Job, JobStatus, Version
from .index_contracts import EngineIndex, IndexInventory, IndexRecipe, IndexStatus


def legacy_graphmind_recipe(version: Version) -> IndexRecipe:
    return IndexRecipe.create("graphmind", "legacy-dense-reference-v1", {
        "extractor": version.extractor_fingerprint, "chunker": version.chunker_fingerprint,
        "embedding": version.embedding_fingerprint})


class IndexRepository:
    def __init__(self, metadata: MetadataStore, sources: SourceService | None = None) -> None:
        self.metadata = metadata
        self.sources = sources or SourceService(metadata)

    @staticmethod
    def _record(row: sqlite3.Row | None) -> EngineIndex | None:
        if row is None:
            return None
        try:
            recipe = IndexRecipe.deserialize(row["recipe_json"])
            if recipe.fingerprint != row["recipe_fingerprint"] or recipe.engine_id != row["engine_id"]:
                raise ValueError("Recipe identity mismatch")
            inventory = (IndexInventory(row["unit_count"], row["unit_digest"])
                         if row["unit_count"] is not None else None)
            return EngineIndex(row["index_id"], row["source_version_id"], recipe,
                               IndexStatus(row["status"]), inventory, row["created_at"],
                               row["updated_at"], row["failure_code"], row["artifact_ref"], row["artifact_digest"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ManifestUnavailableError("Derived-index metadata is invalid") from exc

    def index(self, index_id: str) -> EngineIndex | None:
        with self.metadata.connection() as connection:
            return self._record(connection.execute(
                "SELECT * FROM engine_indexes WHERE index_id = ?", (index_id,)).fetchone())

    def prepare(self, source_version_id: str, recipe: IndexRecipe) -> EngineIndex:
        installation = self.metadata.installation_id()
        with self.metadata.transaction() as connection:
            source = connection.execute(
                """SELECT d.deleted_at FROM versions v JOIN documents d ON d.document_id = v.document_id
                   WHERE v.version_id = ?""", (source_version_id,)).fetchone()
            if source is None or source["deleted_at"] is not None:
                raise ManifestUnavailableError("Index source is unavailable")
            index_id = recipe.index_id(installation, source_version_id)
            self._insert(connection, index_id, source_version_id, recipe, IndexStatus.STAGED)
            result = self._record(connection.execute(
                "SELECT * FROM engine_indexes WHERE index_id = ?", (index_id,)).fetchone())
            if result is None:
                raise ManifestUnavailableError("Derived-index identity conflicts")
            return result

    def submit(self, source_version_id: str, recipe: IndexRecipe, *, max_queued_jobs: int) -> Job:
        source = self.metadata.version(source_version_id)
        if source is None:
            raise PublicationError("Index source is unavailable")
        self.sources.active(source.document_id, source_version_id)
        index = self.prepare(source_version_id, recipe)
        key = f"build_index:{index.index_id}"
        return self.metadata.put_job(Job(
            str(uuid.uuid5(uuid.UUID(self.metadata.installation_id()), key)), key, "build_index",
            source.document_id, source_version_id, JobStatus.QUEUED, 0, utc_now(),
            payload_json=json.dumps({"schema_version": 1, "index_ids": [index.index_id]},
                                    sort_keys=True, separators=(",", ":"))), max_queued_jobs=max_queued_jobs)

    @staticmethod
    def _insert(connection, index_id, source_version_id, recipe, status, inventory=None, failure_code=None):
        now = utc_now()
        connection.execute(
            """INSERT INTO engine_indexes(index_id, source_version_id, engine_id, recipe_fingerprint,
                   recipe_json, status, unit_count, unit_digest, created_at, updated_at, failure_code)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(index_id) DO NOTHING""",
            (index_id, source_version_id, recipe.engine_id, recipe.fingerprint, recipe.serialized,
             status.value, inventory.unit_count if inventory else None,
             inventory.unit_digest if inventory else None, now, now, failure_code))

    @staticmethod
    def _targets(job: Job) -> list[str] | None:
        try:
            value = json.loads(job.payload_json)
            if value == {} and job.operation in {"import_txt", "import_document"}:
                return None
            targets = value["index_ids"]
            if (set(value) != {"schema_version", "index_ids"}
                    or type(value["schema_version"]) is not int or value["schema_version"] != 1
                    or not isinstance(targets, list) or not 1 <= len(targets) <= 16
                    or any(not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{64}", item)
                           for item in targets) or len(set(targets)) != len(targets)):
                raise ValueError("Invalid target list")
            return targets
        except (KeyError, TypeError, ValueError) as exc:
            raise JobInputError("Derived-index job input is invalid") from exc

    def _owned(self, connection, job: Job, index_id: str):
        self.metadata._assert_job_lease(connection, job)
        row = connection.execute(
            """SELECT i.*, v.document_id, d.deleted_at FROM engine_indexes i
               JOIN versions v ON v.version_id = i.source_version_id
               JOIN documents d ON d.document_id = v.document_id WHERE i.index_id = ?""",
            (index_id,)).fetchone()
        if (row is None or row["source_version_id"] != job.version_id
                or row["document_id"] != job.document_id or row["deleted_at"] is not None):
            raise PublicationError("Job does not own an available index source")
        targets = self._targets(job)
        if targets is None:
            version = self.metadata._version(connection.execute(
                "SELECT * FROM versions WHERE version_id = ?", (job.version_id,)).fetchone())
            if row["recipe_json"] != legacy_graphmind_recipe(version).serialized:
                raise PublicationError("Legacy job cannot build another recipe")
        elif index_id not in targets:
            raise PublicationError("Index is outside the job input")
        return row

    def start(self, job: Job, index_id: str) -> EngineIndex:
        with self.metadata.transaction() as connection:
            row = self._owned(connection, job, index_id)
            if row["status"] != IndexStatus.READY.value:
                connection.execute(
                    """UPDATE engine_indexes SET status='building', unit_count=NULL, unit_digest=NULL,
                       failure_code=NULL, updated_at=? WHERE index_id=?""", (utc_now(), index_id))
            return self._record(connection.execute(
                "SELECT * FROM engine_indexes WHERE index_id=?", (index_id,)).fetchone())

    @staticmethod
    def _head(connection, document_id: str, engine_id: str, index_id: str) -> None:
        connection.execute(
            """INSERT INTO engine_index_heads(document_id, engine_id, index_id) VALUES (?, ?, ?)
               ON CONFLICT(document_id, engine_id) DO UPDATE SET index_id=excluded.index_id""",
            (document_id, engine_id, index_id))

    def complete(self, job: Job, index_id: str, inventory: IndexInventory, *, activate=False,
                 artifact_ref: str | None = None, artifact_digest: str | None = None) -> None:
        if artifact_ref is not None:
            if (not artifact_ref.startswith(f"derived/{index_id}/") or "\\" in artifact_ref or ":" in artifact_ref
                    or any(part in {"", ".", ".."} for part in artifact_ref.split("/"))
                    or not isinstance(artifact_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", artifact_digest)):
                raise PublicationError("Derived artifact identity must be private, relative and hashed")
        elif artifact_digest is not None:
            raise PublicationError("Derived artifact reference is missing")
        with self.metadata.transaction() as connection:
            row = self._owned(connection, job, index_id)
            if artifact_ref is None and not (row["engine_id"] == "graphmind"
                    and IndexRecipe.deserialize(row["recipe_json"]).revision == "legacy-dense-reference-v1"):
                raise PublicationError("Derived index requires a verified artifact manifest")
            if row["status"] == IndexStatus.READY.value:
                if ((row["unit_count"], row["unit_digest"], row["artifact_ref"], row["artifact_digest"])
                        != (inventory.unit_count, inventory.unit_digest, artifact_ref, artifact_digest)):
                    raise PublicationError("A ready index inventory is immutable")
            elif row["status"] != IndexStatus.BUILDING.value:
                raise PublicationError("Index must be building before completion")
            connection.execute(
                """UPDATE engine_indexes SET status='ready', unit_count=?, unit_digest=?, artifact_ref=?, artifact_digest=?,
                   failure_code=NULL, updated_at=? WHERE index_id=?""",
                (inventory.unit_count, inventory.unit_digest, artifact_ref, artifact_digest, utc_now(), index_id))
            if activate:
                current = connection.execute(
                    """SELECT 1 FROM documents d JOIN versions v ON v.version_id=d.active_version_id
                       WHERE d.document_id=? AND d.active_version_id=? AND d.deleted_at IS NULL
                       AND v.status='ready'""", (job.document_id, job.version_id)).fetchone()
                if current is None:
                    raise PublicationError("Index source changed before activation")
                self._head(connection, job.document_id, row["engine_id"], index_id)

    def fail(self, job: Job, index_id: str, failure_code: str) -> None:
        if not re.fullmatch(r"[a-z0-9_]{1,64}", failure_code):
            raise ValueError("Index failure requires a safe error code")
        with self.metadata.transaction() as connection:
            row = self._owned(connection, job, index_id)
            if row["status"] != IndexStatus.READY.value:
                connection.execute(
                    "UPDATE engine_indexes SET status='failed', failure_code=?, updated_at=? WHERE index_id=?",
                    (failure_code, utc_now(), index_id))

    def ready(self, document_id: str, engine_id: str) -> EngineIndex | None:
        with self.metadata.connection() as connection:
            return self._record(connection.execute(
                """SELECT i.* FROM engine_index_heads h JOIN engine_indexes i ON i.index_id=h.index_id
                   JOIN documents d ON d.document_id=h.document_id
                   JOIN versions v ON v.version_id=i.source_version_id
                   WHERE h.document_id=? AND h.engine_id=? AND i.engine_id=h.engine_id
                     AND v.document_id=d.document_id AND d.active_version_id=v.version_id
                     AND d.deleted_at IS NULL AND v.status='ready' AND i.status='ready'""",
                (document_id, engine_id)).fetchone())

    def current(self, engine_id: str, *, revision: str | None = None) -> list[EngineIndex]:
        with self.metadata.connection() as connection:
            rows = connection.execute(
                """SELECT i.* FROM engine_index_heads h JOIN engine_indexes i ON i.index_id=h.index_id
                   JOIN versions v ON v.version_id=i.source_version_id
                   JOIN documents d ON d.document_id=h.document_id
                   WHERE h.engine_id=? AND i.engine_id=h.engine_id AND v.document_id=d.document_id
                     AND d.active_version_id=v.version_id AND d.deleted_at IS NULL
                     AND v.status='ready' AND i.status='ready' ORDER BY d.document_id""", (engine_id,))
            result = [self._record(row) for row in rows]
        return [item for item in result if revision is None or item.recipe.revision == revision]

    def activate(self, job: Job) -> None:
        """Publish ready recipes for an existing source without rolling its version back."""
        with self.metadata.transaction() as connection:
            self.metadata._assert_job_lease(connection, job)
            current = connection.execute(
                """SELECT 1 FROM documents d JOIN versions v ON v.version_id=d.active_version_id
                   WHERE d.document_id=? AND d.active_version_id=? AND d.deleted_at IS NULL
                   AND v.status='ready'""", (job.document_id, job.version_id)).fetchone()
            if current is None:
                raise ManifestUnavailableError("Index source changed before activation")
            self.publish_source_heads(connection, job)

    def invalidate_document(self, job: Job) -> None:
        with self.metadata.transaction() as connection:
            self.metadata._assert_job_lease(connection, job)
            row = connection.execute("SELECT deleted_at FROM documents WHERE document_id=?",
                                     (job.document_id,)).fetchone()
            if job.operation != "delete_document" or row is None or row["deleted_at"] is None:
                raise PublicationError("Index invalidation requires the document deletion job")
            connection.execute(
                """UPDATE engine_indexes SET status='invalidated', updated_at=?
                   WHERE source_version_id IN (SELECT version_id FROM versions WHERE document_id=?)""",
                (utc_now(), job.document_id))
            connection.execute("DELETE FROM engine_index_heads WHERE document_id=?", (job.document_id,))

    def _legacy(self, connection, version: Version, *, deleted=False) -> EngineIndex:
        recipe = legacy_graphmind_recipe(version)
        index_id = recipe.index_id(self.metadata._installation_id(connection), version.version_id)
        identifiers = [row[0] for row in connection.execute(
            "SELECT chunk_id FROM chunks WHERE version_id=? ORDER BY ordinal", (version.version_id,))]
        inventory = None
        try:
            inventory = IndexInventory.from_unit_ids(identifiers)
            valid = ((inventory.unit_count, inventory.unit_digest)
                     == (version.expected_chunk_count, version.chunk_digest))
        except ValueError:
            valid = False
        status = IndexStatus.INVALIDATED if deleted else IndexStatus.READY if valid else IndexStatus.FAILED
        self._insert(connection, index_id, version.version_id, recipe, status, inventory,
                     None if valid or deleted else "legacy_manifest_mismatch")
        return self._record(connection.execute(
            "SELECT * FROM engine_indexes WHERE index_id=?", (index_id,)).fetchone())

    def backfill_legacy(self, connection: sqlite3.Connection) -> None:
        for row in connection.execute(
                """SELECT v.*, d.deleted_at, d.active_version_id FROM versions v
                   JOIN documents d ON d.document_id=v.document_id WHERE v.status='ready'""").fetchall():
            index = self._legacy(connection, self.metadata._version(row), deleted=row["deleted_at"] is not None)
            if index.status is IndexStatus.READY and row["active_version_id"] == row["version_id"]:
                self._head(connection, row["document_id"], "graphmind", index.index_id)

    def publish_source_heads(self, connection: sqlite3.Connection, job: Job) -> None:
        targets = self._targets(job)
        if targets is None:
            version = self.metadata._version(connection.execute(
                "SELECT * FROM versions WHERE version_id=?", (job.version_id,)).fetchone())
            index = self._legacy(connection, version)
            if index.status is not IndexStatus.READY:
                raise PublicationError("GraphMind index inventory does not match the source manifest")
            self._head(connection, job.document_id, "graphmind", index.index_id)
            return
        engines = set()
        for index_id in targets:
            row = self._owned(connection, job, index_id)
            if row["status"] != IndexStatus.READY.value or row["engine_id"] in engines:
                raise PublicationError("All selected engines require one ready index before publication")
            engines.add(row["engine_id"])
            self._head(connection, job.document_id, row["engine_id"], index_id)
