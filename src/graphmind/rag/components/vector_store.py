"""Shared vector-store contract with memory and Chroma implementations."""
from __future__ import annotations

import heapq
import math
from collections.abc import Sequence
from typing import Protocol

from ...errors import DependencyUnavailableError, EmbeddingMismatchError, GraphMindError
from .contracts import AdapterStatus, Chunk, SearchHit
from .embeddings import EmbeddingProvider, cosine_similarity


class VectorStore(Protocol):
    @property
    def embedding_fingerprint(self) -> str: ...
    def readiness(self) -> AdapterStatus: ...
    def upsert_version(self, installation_id: str, chunks: Sequence[Chunk]) -> None: ...
    def count_version(self, installation_id: str, version_id: str) -> int: ...
    def chunk_ids_for_version(self, installation_id: str, version_id: str) -> set[str]: ...
    def search(self, installation_id: str, query: str, limit: int,
               *, active_version_ids: set[str] | None = None) -> list[SearchHit]: ...
    def delete_document(self, installation_id: str, document_id: str) -> None: ...
    def close(self) -> None: ...


class MemoryVectorStore:
    def __init__(self, embedding: EmbeddingProvider) -> None:
        self.embedding = embedding
        self._items: dict[tuple[str, str], tuple[str, str, list[float], str]] = {}

    @property
    def embedding_fingerprint(self) -> str:
        return self.embedding.fingerprint

    def readiness(self) -> AdapterStatus:
        return AdapterStatus("memory-vector", True, "ready")

    def upsert_version(self, installation_id: str, chunks: Sequence[Chunk]) -> None:
        vectors = self.embedding.embed_many([chunk.text for chunk in chunks])
        for chunk, vector in zip(chunks, vectors, strict=True):
            self._items[(installation_id, chunk.chunk_id)] = (
                chunk.version_id,
                chunk.document_id,
                vector,
                chunk.text,
            )

    def count_version(self, installation_id: str, version_id: str) -> int:
        return sum(
            1
            for (item_installation, _), (item_version, _, _, _) in self._items.items()
            if item_installation == installation_id and item_version == version_id
        )

    def chunk_ids_for_version(self, installation_id: str, version_id: str) -> set[str]:
        return {
            chunk_id
            for (item_installation, chunk_id), (item_version, _, _, _) in self._items.items()
            if item_installation == installation_id and item_version == version_id
        }

    def search(self, installation_id: str, query: str, limit: int,
               *, active_version_ids: set[str] | None = None) -> list[SearchHit]:
        query_vector = self.embedding.embed(query)
        hits = [
            SearchHit(chunk_id=chunk_id, score=cosine_similarity(query_vector, vector))
            for (item_installation, chunk_id), (version_id, _, vector, _) in self._items.items()
            if item_installation == installation_id
            and (active_version_ids is None or version_id in active_version_ids)
        ]
        return sorted(hits, key=lambda item: (-item.score, item.chunk_id))[:limit]

    def delete_document(self, installation_id: str, document_id: str) -> None:
        for key, (_, item_document, _, _) in list(self._items.items()):
            if key[0] == installation_id and item_document == document_id:
                del self._items[key]

    def close(self) -> None:
        return None


class ChromaVectorStore:
    def __init__(self, path: str, collection_name: str, embedding: EmbeddingProvider, *,
                 host: str | None = None, port: int = 18001) -> None:
        self.path = path
        self.collection_name = collection_name
        self.embedding = embedding
        self.host, self.port = host, port
        self._client = None
        self._collection = None

    @property
    def embedding_fingerprint(self) -> str:
        return self.embedding.fingerprint

    def _get_collection(self):
        if self._collection is not None:
            return self._collection
        try:
            import chromadb
            from chromadb.errors import NotFoundError

            if self.host is None:
                self._client = chromadb.PersistentClient(path=self.path)
            else:
                from .chroma_http import ChromaHTTPClient
                self._client = ChromaHTTPClient(self.host, self.port)
            expected_fingerprint = self.embedding.fingerprint
            try:
                collection = self._client.get_collection(name=self.collection_name)
            except NotFoundError:
                collection = self._client.create_collection(
                    name=self.collection_name,
                    metadata={
                        "hnsw:space": "cosine",
                        "graphmind_embedding": expected_fingerprint,
                    },
                )
            metadata = collection.metadata or {}
            stored_fingerprint = metadata.get("graphmind_embedding")
            if stored_fingerprint != expected_fingerprint:
                raise EmbeddingMismatchError(
                    "Chroma collection embedding fingerprint is missing or does not match configuration"
                )
            self._collection = collection
            return self._collection
        except GraphMindError:
            if self.host is not None:
                self.close()
            raise
        except Exception as exc:
            if self.host is not None:
                self.close()
            raise DependencyUnavailableError("Chroma is unavailable") from exc

    def readiness(self) -> AdapterStatus:
        try:
            self._get_collection().count()
            return AdapterStatus("chroma", True, "ready")
        except GraphMindError as exc:
            return AdapterStatus("chroma", False, str(exc))

    def upsert_version(self, installation_id: str, chunks: Sequence[Chunk]) -> None:
        if not chunks:
            return
        try:
            self._get_collection().upsert(
                ids=[chunk.chunk_id for chunk in chunks],
                embeddings=self.embedding.embed_many([chunk.text for chunk in chunks]),
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
        except GraphMindError:
            raise
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
        except GraphMindError:
            raise
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
        except GraphMindError:
            raise
        except Exception as exc:
            raise DependencyUnavailableError("Chroma chunk-ID read failed") from exc

    def search(self, installation_id: str, query: str, limit: int,
               *, active_version_ids: set[str] | None = None) -> list[SearchHit]:
        if active_version_ids == set():
            return []
        try:
            filters = [{"installation_id": {"$eq": installation_id}}]
            if active_version_ids is not None:
                filters.append({"version_id": {"$in": sorted(active_version_ids)}})
            collection = self._get_collection()
            query_vector = self.embedding.embed(query)
            where = {"$and": filters} if len(filters) > 1 else filters[0]
            result = collection.query(
                query_embeddings=[query_vector],
                n_results=max(1, limit),
                where=where,
                include=["distances"],
            )
            ids = (result.get("ids") or [[]])[0]
            distances = (result.get("distances") or [[]])[0]
            hits = [
                SearchHit(chunk_id=str(chunk_id), score=1.0 - float(distance))
                for chunk_id, distance in zip(ids, distances, strict=True)
            ]
            if active_version_ids is not None and len(hits) < limit:
                # The pinned local Chroma runtime can omit a newly written
                # vector at its HNSW/buffer boundary even when get() finds it.
                # Fill a short filtered result by scanning eligible vectors in
                # bounded pages, retaining only top-k (never historical text).
                best: list[tuple[float, str]] = []
                query_norm = math.sqrt(sum(value * value for value in query_vector))
                offset = 0
                while True:
                    page = collection.get(where=where, include=["embeddings"], limit=256, offset=offset)
                    page_ids = page.get("ids") or []
                    vectors = page.get("embeddings")
                    if vectors is None and page_ids:
                        raise DependencyUnavailableError("Chroma omitted stored embeddings")
                    for chunk_id, raw in zip(page_ids, vectors if vectors is not None else [], strict=True):
                        vector = [float(value) for value in raw]
                        dot = cosine_similarity(query_vector, vector)
                        norm = math.sqrt(sum(value * value for value in vector)) * query_norm
                        score = dot / norm if norm else 0.0
                        best.append((score, str(chunk_id)))
                    best = heapq.nlargest(max(1, limit), best)
                    if len(page_ids) < 256:
                        break
                    offset += len(page_ids)
                hits = [SearchHit(chunk_id=chunk_id, score=score) for score, chunk_id in best]
            return hits
        except GraphMindError:
            raise
        except Exception as exc:
            raise DependencyUnavailableError("Chroma query failed") from exc

    def delete_document(self, installation_id: str, document_id: str) -> None:
        try:
            self._get_collection().delete(
                where={
                    "$and": [
                        {"installation_id": {"$eq": installation_id}},
                        {"document_id": {"$eq": document_id}},
                    ]
                }
            )
        except GraphMindError:
            raise
        except Exception as exc:
            raise DependencyUnavailableError("Chroma document cleanup failed") from exc

    def delete_installation(self, installation_id: str) -> None:
        try:
            self._get_collection().delete(where={"installation_id": {"$eq": installation_id}})
        except GraphMindError:
            raise
        except Exception as exc:
            raise DependencyUnavailableError("Chroma cleanup failed") from exc

    def close(self) -> None:
        self._collection = None
        if self._client is not None and hasattr(self._client, "close"):
            self._client.close()
        self._client = None
