"""Existing active-manifest vector/reference retrieval; ranking is unchanged."""
from __future__ import annotations

from ...errors import EngineUnavailableError, EmbeddingMismatchError, ManifestUnavailableError, QueryBudgetError, RetryableQueryError
from ...metadata import MetadataStore
from ...sources import SourceService
from ..components.contracts import Evidence
from ..components.vector_store import VectorStore
from .reference_graph import GraphStore


class RetrievalService:
    def __init__(
        self,
        metadata: MetadataStore,
        vector: VectorStore,
        graph: GraphStore,
        installation_id: str,
        *,
        seed_limit: int = 4,
        graph_limit: int = 8,
        graph_scope: str = "chunk",
        max_evidence: int = 12,
        min_vector_score: float = 0.02,
        max_question_chars: int = 2000,
        indexes=None,
        sources: SourceService | None = None,
    ) -> None:
        self.metadata = metadata
        self.vector = vector
        self.graph = graph
        self.installation_id = installation_id
        self.seed_limit = seed_limit
        self.graph_limit = graph_limit
        self.graph_scope = graph_scope
        self.max_evidence = max_evidence
        self.min_vector_score = min_vector_score
        self.max_question_chars = max_question_chars
        self.indexes = indexes
        self.sources = sources or SourceService(metadata)

    def retrieve(self, query: str) -> list[Evidence]:
        return self._retrieve(query, ())

    def retrieve_scoped(self, query, context):
        if context.engine_id != "graphmind":
            raise EngineUnavailableError("This backend serves GraphMind only")
        return self._retrieve(query, context.document_ids)

    def _retrieve(self, query: str, document_ids: tuple[str, ...]) -> list[Evidence]:
        if not query.strip():
            return []
        if len(query) > self.max_question_chars:
            raise QueryBudgetError("Question exceeds the configured character budget")
        for identifier in document_ids:
            self.sources.active(identifier)
        if self.indexes is not None:
            versions = [self.metadata.version(item.source_version_id) for item in
                        self.indexes.current("graphmind", revision="legacy-dense-reference-v1")]
            versions = [item for item in versions if not document_ids or item.document_id in document_ids]
            if set(document_ids) - {item.document_id for item in versions}:
                raise ManifestUnavailableError("Selected documents have no ready legacy GraphMind index")
            active_versions = {item.version_id for item in versions}
            active_fingerprints = {item.embedding_fingerprint for item in versions}
        elif document_ids:
            versions = [self.sources.active(identifier)[1] for identifier in document_ids]
            active_versions = {item.version_id for item in versions}
            active_fingerprints = {item.embedding_fingerprint for item in versions}
        else:
            active_versions = self.metadata.active_version_ids()
            active_fingerprints = self.metadata.active_embedding_fingerprints()
        incompatible = active_fingerprints - {self.vector.embedding_fingerprint}
        if incompatible:
            raise EmbeddingMismatchError(
                "Active documents use a different embedding fingerprint; reindex them explicitly"
            )
        hits = self.vector.search(
            self.installation_id,
            query,
            limit=max(self.seed_limit * 6, self.max_evidence),
            active_version_ids=active_versions,
        )
        hit_ids = [hit.chunk_id for hit in hits]
        active_hits = self.metadata.active_chunks(hit_ids)
        eligible_hits = [
            hit
            for hit in hits
            if hit.chunk_id in active_hits and hit.score >= self.min_vector_score
        ][: self.seed_limit]
        if not eligible_hits:
            return []

        seed_ids = [hit.chunk_id for hit in eligible_hits]
        graph_ids = self.graph.expand(
            self.installation_id,
            seed_ids,
            self.graph_limit,
            self.graph_scope,
            active_version_ids=active_versions,
        )
        graph_active = self.metadata.active_chunks(graph_ids)
        evidence: list[Evidence] = []
        seen: set[str] = set()
        score_by_id = {hit.chunk_id: hit.score for hit in eligible_hits}
        for via, candidate_ids in (("vector", seed_ids), ("graph", graph_ids)):
            records = active_hits if via == "vector" else graph_active
            for chunk_id in candidate_ids:
                if chunk_id in seen or chunk_id not in records:
                    continue
                chunk, document = records[chunk_id]
                evidence.append(
                    Evidence(
                        citation_id=chunk.chunk_id,
                        document_id=chunk.document_id,
                        version_id=chunk.version_id,
                        display_name=document.display_name,
                        locator=chunk.locator,
                        excerpt=chunk.text[:800],
                        via=via,
                        score=score_by_id.get(chunk_id, 0.0),
                    )
                )
                seen.add(chunk_id)
                if len(evidence) >= self.max_evidence:
                    return self._validate_evidence(evidence)
        return self._validate_evidence(evidence)

    def _validate_evidence(self, evidence: list[Evidence]) -> list[Evidence]:
        active_versions = self.metadata.active_version_ids()
        if any(item.version_id not in active_versions for item in evidence):
            raise RetryableQueryError("A source version changed during retrieval; retry")
        return evidence
