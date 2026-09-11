"""Vector and graph adapter interfaces with memory and real implementations."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from .domain import AdapterStatus, Chunk, Document, SearchHit, Version
from .embeddings import HashEmbedding, cosine_similarity
from .errors import DependencyUnavailableError


class VectorStore(Protocol):
    def readiness(self) -> AdapterStatus: ...
    def upsert_version(self, installation_id: str, chunks: Sequence[Chunk]) -> None: ...
    def count_version(self, installation_id: str, version_id: str) -> int: ...
    def chunk_ids_for_version(self, installation_id: str, version_id: str) -> set[str]: ...
    def search(self, installation_id: str, query: str, limit: int) -> list[SearchHit]: ...
    def close(self) -> None: ...


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
    def expand(self, installation_id: str, seed_chunk_ids: Sequence[str], limit: int) -> list[str]: ...
    def close(self) -> None: ...


class MemoryVectorStore:
    def __init__(self, embedding: HashEmbedding) -> None:
        self.embedding = embedding
        self._items: dict[tuple[str, str], tuple[str, list[float], str]] = {}

    def readiness(self) -> AdapterStatus:
        return AdapterStatus("memory-vector", True, "ready")

    def upsert_version(self, installation_id: str, chunks: Sequence[Chunk]) -> None:
        for chunk in chunks:
            self._items[(installation_id, chunk.chunk_id)] = (
                chunk.version_id,
                self.embedding.embed(chunk.text),
                chunk.text,
            )

    def count_version(self, installation_id: str, version_id: str) -> int:
        return sum(
            1
            for (item_installation, _), (item_version, _, _) in self._items.items()
            if item_installation == installation_id and item_version == version_id
        )

    def chunk_ids_for_version(self, installation_id: str, version_id: str) -> set[str]:
        return {
            chunk_id
            for (item_installation, chunk_id), (item_version, _, _) in self._items.items()
            if item_installation == installation_id and item_version == version_id
        }

    def search(self, installation_id: str, query: str, limit: int) -> list[SearchHit]:
        query_vector = self.embedding.embed(query)
        hits = [
            SearchHit(chunk_id=chunk_id, score=cosine_similarity(query_vector, vector))
            for (item_installation, chunk_id), (_, vector, _) in self._items.items()
            if item_installation == installation_id
        ]
        return sorted(hits, key=lambda item: (-item.score, item.chunk_id))[:limit]

    def close(self) -> None:
        return None


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

    def expand(self, installation_id: str, seed_chunk_ids: Sequence[str], limit: int) -> list[str]:
        target_names: set[str] = set()
        for chunk_id in seed_chunk_ids:
            chunk = self._chunks.get((installation_id, chunk_id))
            if chunk:
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
        )
        return result[:limit]

    def close(self) -> None:
        return None


class ChromaVectorStore:
    def __init__(self, path: str, collection_name: str, embedding: HashEmbedding) -> None:
        self.path = path
        self.collection_name = collection_name
        self.embedding = embedding
        self._client = None
        self._collection = None

    def _get_collection(self):
        if self._collection is not None:
            return self._collection
        try:
            import chromadb

            self._client = chromadb.PersistentClient(path=self.path)
            self._collection = self._client.get_or_create_collection(
                name=self.collection_name,
                metadata={"hnsw:space": "cosine", "graphmind_embedding": self.embedding.fingerprint},
            )
            metadata = self._collection.metadata or {}
            stored_fingerprint = metadata.get("graphmind_embedding")
            if stored_fingerprint and stored_fingerprint != self.embedding.fingerprint:
                raise DependencyUnavailableError(
                    "Chroma collection embedding fingerprint does not match configuration"
                )
            return self._collection
        except Exception as exc:
            raise DependencyUnavailableError("Chroma is unavailable") from exc

    def readiness(self) -> AdapterStatus:
        try:
            self._get_collection().count()
            return AdapterStatus("chroma", True, "ready")
        except DependencyUnavailableError as exc:
            return AdapterStatus("chroma", False, str(exc))

    def upsert_version(self, installation_id: str, chunks: Sequence[Chunk]) -> None:
        if not chunks:
            return
        try:
            self._get_collection().upsert(
                ids=[chunk.chunk_id for chunk in chunks],
                embeddings=[self.embedding.embed(chunk.text) for chunk in chunks],
                documents=[chunk.text for chunk in chunks],
                metadatas=[
                    {
                        "installation_id": installation_id,
                        "version_id": chunk.version_id,
                        "document_id": chunk.document_id,
                        "ordinal": chunk.ordinal,
                        "locator": chunk.locator,
                    }
                    for chunk in chunks
                ],
            )
        except Exception as exc:
            raise DependencyUnavailableError("Chroma version write failed") from exc

    def count_version(self, installation_id: str, version_id: str) -> int:
        try:
            result = self._get_collection().get(
                where={
                    "$and": [
                        {"installation_id": {"$eq": installation_id}},
                        {"version_id": {"$eq": version_id}},
                    ]
                },
                include=[],
            )
            return len(result.get("ids") or [])
        except Exception as exc:
            raise DependencyUnavailableError("Chroma count failed") from exc

    def chunk_ids_for_version(self, installation_id: str, version_id: str) -> set[str]:
        try:
            result = self._get_collection().get(
                where={
                    "$and": [
                        {"installation_id": {"$eq": installation_id}},
                        {"version_id": {"$eq": version_id}},
                    ]
                },
                include=[],
            )
            return {str(chunk_id) for chunk_id in (result.get("ids") or [])}
        except Exception as exc:
            raise DependencyUnavailableError("Chroma chunk-ID read failed") from exc

    def search(self, installation_id: str, query: str, limit: int) -> list[SearchHit]:
        try:
            result = self._get_collection().query(
                query_embeddings=[self.embedding.embed(query)],
                n_results=max(1, limit),
                where={"installation_id": {"$eq": installation_id}},
                include=["distances"],
            )
            ids = (result.get("ids") or [[]])[0]
            distances = (result.get("distances") or [[]])[0]
            return [
                SearchHit(chunk_id=str(chunk_id), score=1.0 - float(distance))
                for chunk_id, distance in zip(ids, distances, strict=True)
            ]
        except Exception as exc:
            raise DependencyUnavailableError("Chroma query failed") from exc

    def delete_installation(self, installation_id: str) -> None:
        try:
            self._get_collection().delete(where={"installation_id": {"$eq": installation_id}})
        except Exception as exc:
            raise DependencyUnavailableError("Chroma cleanup failed") from exc

    def close(self) -> None:
        self._collection = None
        if self._client is not None and hasattr(self._client, "close"):
            self._client.close()
        self._client = None


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
        MATCH (source:GraphMindChunk {installation_id: $installation_id})-[old:REFERENCES]->()
        DELETE old
        """
        resolve_query = """
        MATCH (source:GraphMindChunk {installation_id: $installation_id})
        UNWIND source.reference_names AS target_name
        MATCH (target:GraphMindDocument {
            installation_id: $installation_id,
            normalized_name: target_name
        })
        MERGE (source)-[:REFERENCES]->(target)
        RETURN count(*) AS resolved
        """
        try:
            driver = self._get_driver()
            driver.execute_query(
                query,
                installation_id=installation_id,
                document_id=document.document_id,
                display_name=document.display_name,
                normalized_name=document.normalized_name,
                collection_id=document.collection_id,
                version_id=version.version_id,
                chunks=rows,
                database_=self.database,
            )
            driver.execute_query(
                delete_references_query,
                installation_id=installation_id,
                database_=self.database,
            )
            driver.execute_query(
                resolve_query,
                installation_id=installation_id,
                database_=self.database,
            )
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

    def expand(self, installation_id: str, seed_chunk_ids: Sequence[str], limit: int) -> list[str]:
        if not seed_chunk_ids:
            return []
        try:
            records, _, _ = self._get_driver().execute_query(
                """
                UNWIND $seed_ids AS seed_id
                MATCH (source:GraphMindChunk {
                    installation_id: $installation_id,
                    chunk_id: seed_id
                })-[:REFERENCES]->(target:GraphMindDocument)
                MATCH (target)-[:HAS_VERSION]->(:GraphMindVersion)-[:HAS_CHUNK]->(chunk:GraphMindChunk)
                RETURN DISTINCT chunk.chunk_id AS chunk_id
                ORDER BY chunk_id
                LIMIT $limit
                """,
                installation_id=installation_id,
                seed_ids=list(seed_chunk_ids),
                limit=int(limit),
                database_=self.database,
            )
            return [str(record["chunk_id"]) for record in records]
        except Exception as exc:
            raise DependencyUnavailableError("Neo4j graph expansion failed") from exc

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
