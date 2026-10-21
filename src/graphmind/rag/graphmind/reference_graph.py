"""GraphMind's explicit document references, not an entity/community graph."""
from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from ...errors import DependencyUnavailableError
from ..components.contracts import AdapterStatus, Chunk, Document, Version


class GraphStore(Protocol):
    def readiness(self) -> AdapterStatus: ...
    def upsert_version(
        self,
        installation_id: str,
        document: Document,
        version: Version,
        chunks: Sequence[Chunk],
    ) -> None: ...
    def count_version(self, installation_id: str, version_id: str) -> int: ...
    def chunk_ids_for_version(self, installation_id: str, version_id: str) -> set[str]: ...
    def expand(
        self,
        installation_id: str,
        seed_chunk_ids: Sequence[str],
        limit: int,
        scope: str = "chunk",
        *, active_version_ids: set[str] | None = None,
    ) -> list[str]: ...
    def delete_document(self, installation_id: str, document_id: str) -> None: ...
    def reference_targets(self, installation_id: str, seed_chunk_ids: Sequence[str], *,
                          active_version_ids: set[str], limit: int = 8) -> list[str]: ...
    def close(self) -> None: ...


class MemoryGraphStore:
    def __init__(self) -> None:
        self._documents: dict[tuple[str, str], Document] = {}
        self._chunks: dict[tuple[str, str], Chunk] = {}

    def readiness(self) -> AdapterStatus:
        return AdapterStatus("memory-graph", True, "ready")

    def upsert_version(
        self,
        installation_id: str,
        document: Document,
        version: Version,
        chunks: Sequence[Chunk],
    ) -> None:
        self._documents[(installation_id, document.document_id)] = document
        for chunk in chunks:
            self._chunks[(installation_id, chunk.chunk_id)] = chunk

    def count_version(self, installation_id: str, version_id: str) -> int:
        return sum(
            1
            for (item_installation, _), chunk in self._chunks.items()
            if item_installation == installation_id and chunk.version_id == version_id
        )

    def chunk_ids_for_version(self, installation_id: str, version_id: str) -> set[str]:
        return {
            chunk_id
            for (item_installation, chunk_id), chunk in self._chunks.items()
            if item_installation == installation_id and chunk.version_id == version_id
        }

    def expand(
        self,
        installation_id: str,
        seed_chunk_ids: Sequence[str],
        limit: int,
        scope: str = "chunk",
        *, active_version_ids: set[str] | None = None,
    ) -> list[str]:
        if limit <= 0:
            return []
        seed_chunks = [
            self._chunks[(installation_id, chunk_id)]
            for chunk_id in seed_chunk_ids
            if (installation_id, chunk_id) in self._chunks
            and (active_version_ids is None
                 or self._chunks[(installation_id, chunk_id)].version_id in active_version_ids)
        ]
        reference_sources = seed_chunks
        if scope == "document":
            source_versions = {(chunk.document_id, chunk.version_id) for chunk in seed_chunks}
            reference_sources = [
                chunk
                for (item_installation, _), chunk in self._chunks.items()
                if item_installation == installation_id
                and (chunk.document_id, chunk.version_id) in source_versions
            ]
        target_names: set[str] = set()
        for chunk in reference_sources:
            target_names.update(chunk.reference_names)
        target_documents = {
            document.document_id
            for (item_installation, _), document in self._documents.items()
            if item_installation == installation_id and document.normalized_name in target_names
        }
        result = sorted(
            chunk.chunk_id
            for (item_installation, _), chunk in self._chunks.items()
            if item_installation == installation_id and chunk.document_id in target_documents
            and (active_version_ids is None or chunk.version_id in active_version_ids)
        )
        return result[:limit]

    def delete_document(self, installation_id: str, document_id: str) -> None:
        self._documents.pop((installation_id, document_id), None)
        for key, chunk in list(self._chunks.items()):
            if key[0] == installation_id and chunk.document_id == document_id:
                del self._chunks[key]

    def reference_targets(self, installation_id: str, seed_chunk_ids: Sequence[str], *,
                          active_version_ids: set[str], limit: int = 8) -> list[str]:
        seeds = [self._chunks[(installation_id, value)] for value in seed_chunk_ids
                 if (installation_id, value) in self._chunks
                 and self._chunks[(installation_id, value)].version_id in active_version_ids]
        versions = {item.version_id for item in seeds}
        names = {name for (installation, _), chunk in self._chunks.items()
                 if installation == installation_id and chunk.version_id in versions
                 for name in chunk.reference_names}
        active_documents = {chunk.document_id for (installation, _), chunk in self._chunks.items()
                            if installation == installation_id and chunk.version_id in active_version_ids}
        return sorted({document.document_id for (installation, _), document in self._documents.items()
                       if installation == installation_id and document.normalized_name in names
                       and document.document_id in active_documents})[:max(0, limit)]

    def close(self) -> None:
        return None


class Neo4jGraphStore:
    def __init__(self, uri: str, username: str, password: str, database: str = "neo4j") -> None:
        self.uri = uri
        self.username = username
        self.password = password
        self.database = database
        self._driver = None

    def _get_driver(self):
        if self._driver is not None:
            return self._driver
        if not self.password:
            raise DependencyUnavailableError("Neo4j password is not configured")
        try:
            from neo4j import GraphDatabase

            self._driver = GraphDatabase.driver(
                self.uri,
                auth=(self.username, self.password),
                connection_timeout=5.0,
            )
            return self._driver
        except Exception as exc:
            raise DependencyUnavailableError("Neo4j driver initialization failed") from exc

    def readiness(self) -> AdapterStatus:
        try:
            self._get_driver().verify_connectivity()
            return AdapterStatus("neo4j", True, "ready")
        except Exception as exc:
            return AdapterStatus("neo4j", False, f"unavailable: {type(exc).__name__}")

    def upsert_version(
        self,
        installation_id: str,
        document: Document,
        version: Version,
        chunks: Sequence[Chunk],
    ) -> None:
        rows = [
            {
                "chunk_id": chunk.chunk_id,
                "ordinal": chunk.ordinal,
                "locator": chunk.locator,
                "reference_names": list(chunk.reference_names),
            }
            for chunk in chunks
        ]
        query = """
        MERGE (d:GraphMindDocument {installation_id: $installation_id, document_id: $document_id})
        SET d.display_name = $display_name,
            d.normalized_name = $normalized_name,
            d.collection_id = $collection_id
        MERGE (v:GraphMindVersion {installation_id: $installation_id, version_id: $version_id})
        SET v.document_id = $document_id
        MERGE (d)-[:HAS_VERSION]->(v)
        WITH v
        UNWIND $chunks AS row
        MERGE (c:GraphMindChunk {installation_id: $installation_id, chunk_id: row.chunk_id})
        SET c.version_id = $version_id,
            c.document_id = $document_id,
            c.ordinal = row.ordinal,
            c.locator = row.locator,
            c.reference_names = row.reference_names
        MERGE (v)-[:HAS_CHUNK]->(c)
        """
        delete_references_query = """
        MATCH (source:GraphMindChunk {installation_id: $installation_id})
              -[old:REFERENCES]->(target:GraphMindDocument {installation_id: $installation_id})
        WHERE source.version_id = $version_id OR target.document_id = $document_id
        DELETE old
        """
        resolve_query = """
        MATCH (source:GraphMindChunk {installation_id: $installation_id})
        UNWIND source.reference_names AS target_name
        MATCH (target:GraphMindDocument {
            installation_id: $installation_id,
            normalized_name: target_name
        })
        WHERE source.version_id = $version_id OR target.document_id = $document_id
        MERGE (source)-[:REFERENCES]->(target)
        RETURN count(*) AS resolved
        """
        try:
            driver = self._get_driver()

            def write_version(transaction):
                transaction.run(
                    query,
                    installation_id=installation_id,
                    document_id=document.document_id,
                    display_name=document.display_name,
                    normalized_name=document.normalized_name,
                    collection_id=document.collection_id,
                    version_id=version.version_id,
                    chunks=rows,
                ).consume()
                for statement in (delete_references_query, resolve_query):
                    transaction.run(
                        statement,
                        installation_id=installation_id,
                        document_id=document.document_id,
                        version_id=version.version_id,
                    ).consume()

            # Publish the nodes and only their affected references together.
            # Readers see the old or new graph; failed resolution rolls back.
            with driver.session(database=self.database) as session:
                session.execute_write(write_version)
        except DependencyUnavailableError:
            raise
        except Exception as exc:
            raise DependencyUnavailableError("Neo4j version write failed") from exc

    def count_version(self, installation_id: str, version_id: str) -> int:
        try:
            records, _, _ = self._get_driver().execute_query(
                """
                MATCH (:GraphMindVersion {installation_id: $installation_id, version_id: $version_id})
                      -[:HAS_CHUNK]->(c:GraphMindChunk)
                RETURN count(DISTINCT c) AS count
                """,
                installation_id=installation_id,
                version_id=version_id,
                database_=self.database,
            )
            return int(records[0]["count"]) if records else 0
        except Exception as exc:
            raise DependencyUnavailableError("Neo4j count failed") from exc

    def chunk_ids_for_version(self, installation_id: str, version_id: str) -> set[str]:
        try:
            records, _, _ = self._get_driver().execute_query(
                """
                MATCH (:GraphMindVersion {installation_id: $installation_id, version_id: $version_id})
                      -[:HAS_CHUNK]->(c:GraphMindChunk)
                RETURN DISTINCT c.chunk_id AS chunk_id
                """,
                installation_id=installation_id,
                version_id=version_id,
                database_=self.database,
            )
            return {str(record["chunk_id"]) for record in records}
        except Exception as exc:
            raise DependencyUnavailableError("Neo4j chunk-ID read failed") from exc

    def expand(
        self,
        installation_id: str,
        seed_chunk_ids: Sequence[str],
        limit: int,
        scope: str = "chunk",
        *, active_version_ids: set[str] | None = None,
    ) -> list[str]:
        if not seed_chunk_ids or limit <= 0 or active_version_ids == set():
            return []
        if scope == "document":
            relation = """
                MATCH (seed:GraphMindChunk {
                    installation_id: $installation_id,
                    chunk_id: seed_id
                })
                MATCH (source:GraphMindChunk {
                    installation_id: $installation_id,
                    document_id: seed.document_id,
                    version_id: seed.version_id
                })-[:REFERENCES]->(target:GraphMindDocument)
            """
        else:
            relation = """
                MATCH (source:GraphMindChunk {
                    installation_id: $installation_id,
                    chunk_id: seed_id
                })-[:REFERENCES]->(target:GraphMindDocument)
            """
        try:
            records, _, _ = self._get_driver().execute_query(
                f"""
                UNWIND $seed_ids AS seed_id
                {relation}
                MATCH (target)-[:HAS_VERSION]->(:GraphMindVersion)-[:HAS_CHUNK]->(chunk:GraphMindChunk)
                WHERE ($active_versions IS NULL OR chunk.version_id IN $active_versions)
                  AND ($active_versions IS NULL OR source.version_id IN $active_versions)
                RETURN DISTINCT chunk.chunk_id AS chunk_id
                ORDER BY chunk_id
                LIMIT $limit
                """,
                installation_id=installation_id,
                seed_ids=list(seed_chunk_ids),
                limit=int(limit),
                active_versions=None if active_version_ids is None else sorted(active_version_ids),
                database_=self.database,
            )
            return [str(record["chunk_id"]) for record in records]
        except Exception as exc:
            raise DependencyUnavailableError("Neo4j graph expansion failed") from exc

    def delete_document(self, installation_id: str, document_id: str) -> None:
        try:
            self._get_driver().execute_query(
                """
                MATCH (node)
                WHERE (node:GraphMindDocument OR node:GraphMindVersion OR node:GraphMindChunk)
                  AND node.installation_id = $installation_id
                  AND node.document_id = $document_id
                DETACH DELETE node
                """,
                installation_id=installation_id,
                document_id=document_id,
                database_=self.database,
            )
        except Exception as exc:
            raise DependencyUnavailableError("Neo4j document cleanup failed") from exc

    def reference_targets(self, installation_id: str, seed_chunk_ids: Sequence[str], *,
                          active_version_ids: set[str], limit: int = 8) -> list[str]:
        if not seed_chunk_ids or not active_version_ids or limit <= 0:
            return []
        try:
            records, _, _ = self._get_driver().execute_query(
                """UNWIND $seed_ids AS seed_id
                   MATCH (seed:GraphMindChunk {installation_id:$installation_id,chunk_id:seed_id})
                   WHERE seed.version_id IN $active_versions
                   MATCH (source:GraphMindChunk {installation_id:$installation_id,
                          document_id:seed.document_id,version_id:seed.version_id})
                         -[:REFERENCES]->(target:GraphMindDocument {installation_id:$installation_id})
                   MATCH (target)-[:HAS_VERSION]->(version:GraphMindVersion)
                   WHERE version.version_id IN $active_versions
                   RETURN DISTINCT target.document_id AS document_id ORDER BY document_id LIMIT $limit""",
                installation_id=installation_id, seed_ids=list(seed_chunk_ids),
                active_versions=sorted(active_version_ids), limit=int(limit), database_=self.database)
            return [str(row["document_id"]) for row in records]
        except Exception as exc:
            raise DependencyUnavailableError("Neo4j reference-target retrieval failed") from exc

    def delete_installation(self, installation_id: str) -> None:
        try:
            self._get_driver().execute_query(
                """
                MATCH (node)
                WHERE (node:GraphMindDocument OR node:GraphMindVersion OR node:GraphMindChunk)
                  AND node.installation_id = $installation_id
                DETACH DELETE node
                """,
                installation_id=installation_id,
                database_=self.database,
            )
        except Exception as exc:
            raise DependencyUnavailableError("Neo4j test cleanup failed") from exc

    def close(self) -> None:
        if self._driver is not None:
            self._driver.close()
            self._driver = None
