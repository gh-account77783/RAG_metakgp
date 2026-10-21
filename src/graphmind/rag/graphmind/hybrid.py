"""Contextual dense/BM25 fusion and query-aware explicit document references."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import math
import re

from ...errors import EngineUnavailableError, EmbeddingMismatchError, ManifestUnavailableError, PublicationError, QueryBudgetError
from ...sources import SourceService
from ..components.artifacts import DerivedArtifactStore
from ..components.contracts import Evidence, Job
from ..components.index_contracts import IndexInventory, IndexRecipe, IndexStatus
from ..components.indexes import IndexRepository
from ..components.ranking import reciprocal_rank_fusion
from ..components.vector_store import VectorStore
from .lexical import LexicalIndex
from .reference_graph import GraphStore


REVISION = "hybrid-context-v1"


class HybridGraphMind:
    def __init__(self, indexes: IndexRepository, sources: SourceService, artifacts: DerivedArtifactStore,
                 vector: VectorStore, graph: GraphStore, installation_id: str, reranker, *,
                 max_evidence: int = 12, max_question_chars: int = 2000, min_relevance: float = 0.05,
                 min_vector_score: float = 0.02, seed_limit: int = 8, graph_limit: int = 8):
        self.indexes, self.sources, self.artifacts = indexes, sources, artifacts
        self.vector, self.graph, self.installation_id = vector, graph, installation_id
        self.reranker = reranker
        self.lexical = LexicalIndex(indexes)
        self.max_evidence, self.max_question_chars = max_evidence, max_question_chars
        self.min_relevance = min_relevance
        self.seed_limit, self.graph_limit, self.graph_scope = seed_limit, graph_limit, "document"
        self.min_vector_score = min_vector_score

    def identity(self) -> dict:
        return {"profile": "hybrid", "index_recipe": self.recipe().fingerprint,
                "reranker": (self.reranker.identity() if hasattr(self.reranker, "identity")
                             else {"implementation": type(self.reranker).__name__}),
                "candidate_limit": 50, "rrf_k": 60, "min_relevance": self.min_relevance,
                "reference_hops": 1, "per_source_evidence_limit": 3}

    def recipe(self) -> IndexRecipe:
        return IndexRecipe.create("graphmind", REVISION,
            {"embedding": self.vector.embedding_fingerprint, "context": "title-locator-heading-next-v1"})

    def build(self, job: Job, index_id: str) -> None:
        index = self.indexes.index(index_id)
        if index is None or index.recipe != self.recipe():
            raise PublicationError("Index recipe differs from the configured GraphMind backend")
        snapshot = self.sources.snapshot_for_job(job)
        chunks = self.indexes.metadata.all_chunks_for_version(job.version_id)
        inventory = IndexInventory.from_unit_ids([item.chunk_id for item in chunks])
        if (inventory.unit_count, inventory.unit_digest) != (
                snapshot.version.expected_chunk_count, snapshot.version.chunk_digest):
            raise ManifestUnavailableError("Source chunk inventory differs")
        if index.status is IndexStatus.READY:
            expected = {item["unit_id"] for item in json.loads(self.artifacts.read(index)["passages.json"])}
            if self.vector.chunk_ids_for_version(self.installation_id, index_id) != expected:
                raise ManifestUnavailableError("Ready dense index inventory differs")
            return
        self.indexes.start(job, index_id)
        try:
            passages, vectors = [], []
            for position, chunk in enumerate(chunks):
                body, locator = chunk.text, chunk.locator
                # A standalone heading carries its following original passage.
                if locator.startswith('heading "') and position + 1 < len(chunks):
                    following = chunks[position + 1]
                    body += "\n" + following.text
                    locator += "; " + following.locator
                unit_id = hashlib.sha256(f"{index_id}\0{chunk.chunk_id}".encode()).hexdigest()
                passages.append({"unit_id": unit_id, "origin_chunk_id": chunk.chunk_id,
                                 "locator": locator, "text": body})
                context = f"Source: {snapshot.document.display_name}\nLocation: {locator}\n\n{body}"
                vectors.append(replace(chunk, chunk_id=unit_id, version_id=index_id, text=context))
            self.indexes.metadata.assert_job_lease(job)
            self.vector.upsert_version(self.installation_id, vectors)
            if self.vector.chunk_ids_for_version(self.installation_id, index_id) != {item.chunk_id for item in vectors}:
                raise PublicationError("Dense inventory differs from the derived index")
            self.indexes.metadata.assert_job_lease(job)
            self.graph.upsert_version(self.installation_id, snapshot.document, snapshot.version, chunks)
            if self.graph.chunk_ids_for_version(self.installation_id, job.version_id) != {item.chunk_id for item in chunks}:
                raise PublicationError("Reference inventory differs from the source")
            self.lexical.replace(job, index_id, snapshot.document.display_name, passages)
            manifest = self.artifacts.publish(job, index_id, [item["unit_id"] for item in passages],
                {"passages.json": json.dumps(passages, ensure_ascii=False).encode("utf-8")})
            self.indexes.complete(job, index_id, manifest.inventory,
                                  artifact_ref=manifest.reference, artifact_digest=manifest.digest)
        except Exception as exc:
            from ...errors import JobLeaseError
            if not isinstance(exc, JobLeaseError):
                self.indexes.fail(job, index_id, getattr(exc, "code", "internal_error"))
            raise

    def audit(self) -> None:
        heads = self.indexes.current("graphmind", revision=REVISION)
        for index in heads:
            payload = json.loads(self.artifacts.read(index)["passages.json"])
            expected = {item["unit_id"] for item in payload}
            if (IndexInventory.from_unit_ids([item["unit_id"] for item in payload]) != index.inventory
                    or self.vector.chunk_ids_for_version(self.installation_id, index.index_id) != expected
                    or set(self.lexical.owners(list(expected), index_ids={index.index_id})) != expected
                    or self.graph.chunk_ids_for_version(self.installation_id, index.source_version_id)
                       != {item["origin_chunk_id"] for item in payload}):
                raise ManifestUnavailableError("Derived GraphMind inventories differ")

    def _pool(self, query: str, heads: dict, cache: dict) -> list[dict]:
        identifiers = set(heads)
        lexical = self.lexical.search(query, index_ids=identifiers, limit=50)
        dense = [item for item in self.vector.search(self.installation_id, query, 50, active_version_ids=identifiers)
                 if item.score >= self.min_vector_score]
        fused = reciprocal_rank_fusion([item.chunk_id for item in lexical],
                                        [item.chunk_id for item in dense], limit=50)
        owners = self.lexical.owners(fused, index_ids=identifiers)
        if set(fused) - set(owners):
            raise ManifestUnavailableError("Retrieval cache and dense inventory differ")
        records = []
        for identifier in fused:
            owner = owners[identifier]
            if owner not in cache:
                index = heads[owner]
                payload = json.loads(self.artifacts.read(index)["passages.json"])
                inventory = IndexInventory.from_unit_ids([item["unit_id"] for item in payload])
                if inventory != index.inventory:
                    raise ManifestUnavailableError("Passage inventory differs from the derived index")
                document, version = self.sources.active(
                    self.indexes.metadata.version(index.source_version_id).document_id, index.source_version_id)
                cache[owner] = {item["unit_id"]: {**item, "document": document, "version": version}
                                for item in payload}
            if identifier not in cache[owner]:
                raise ManifestUnavailableError("Retrieved passage is outside its authenticated inventory")
            records.append(cache[owner][identifier])
        if records:
            self.sources.assert_current(item["version"].version_id for item in records)
            scores = self.reranker.score(query, [
                f'{item["document"].display_name}\n{item["locator"]}\n{item["text"]}' for item in records])
            if len(scores) != len(records) or any(not math.isfinite(float(value)) for value in scores):
                raise ManifestUnavailableError("Reranker returned an invalid inventory")
            for item, score in zip(records, scores, strict=True):
                item["score"] = float(score)
        return sorted(records, key=lambda item: (-item["score"], item["unit_id"]))

    def retrieve_scoped(self, query, context):
        if context.engine_id != "graphmind":
            raise EngineUnavailableError("This backend serves GraphMind only")
        return self.retrieve(query, document_ids=context.document_ids)

    def retrieve(self, query: str, *, document_ids: tuple[str, ...] = ()) -> list[Evidence]:
        if not query.strip():
            return []
        if len(query) > self.max_question_chars:
            raise QueryBudgetError("Question exceeds the configured character budget")
        for identifier in document_ids:
            self.sources.active(identifier)
        heads = {item.index_id: item for item in self.indexes.current("graphmind", revision=REVISION)}
        if document_ids:
            heads = {key: item for key, item in heads.items()
                     if self.indexes.metadata.version(item.source_version_id).document_id in document_ids}
            if set(document_ids) - {self.indexes.metadata.version(item.source_version_id).document_id
                                    for item in heads.values()}:
                raise ManifestUnavailableError("Selected documents have no ready hybrid index")
        expected = self.recipe().fingerprint
        if any(item.recipe.fingerprint != expected for item in heads.values()):
            raise EmbeddingMismatchError("Active GraphMind recipes differ; reindex explicitly")
        if not heads:
            if self.indexes.metadata.active_version_ids():
                raise ManifestUnavailableError("GraphMind hybrid indexes have not been built")
            return []
        cache = {}
        seeds = self._pool(query, heads, cache)
        chosen, counts = [], {}
        for item in seeds:
            document_id = item["document"].document_id
            if item["score"] >= self.min_relevance and counts.get(document_id, 0) < 2:
                chosen.append(item)
                counts[document_id] = counts.get(document_id, 0) + 1
            if len(chosen) >= self.seed_limit:
                break
        if not chosen:
            return []
        source_versions = {item.source_version_id for item in heads.values()}
        targets = self.graph.reference_targets(self.installation_id,
            [item["origin_chunk_id"] for item in chosen], active_version_ids=source_versions, limit=self.graph_limit)
        target_heads = {key: value for key, value in heads.items()
                        if self.indexes.metadata.version(value.source_version_id).document_id in targets}
        linked = self._pool(query, target_heads, cache) if target_heads else []
        linked_ids = {item["unit_id"] for item in linked}
        evidence, seen, counts = [], set(), {}
        for item in sorted(chosen + linked, key=lambda item: (-item["score"], item["unit_id"])):
            body_key = re.sub(r"\s+", " ", item["text"].strip().casefold())
            document = item["document"]
            if (body_key in seen or item["score"] < self.min_relevance
                    or counts.get(document.document_id, 0) >= 3):
                continue
            evidence.append(Evidence(item["unit_id"], document.document_id, item["version"].version_id,
                document.display_name, item["locator"], item["text"],
                "graph" if item["unit_id"] in linked_ids else "hybrid", item["score"]))
            seen.add(body_key)
            counts[document.document_id] = counts.get(document.document_id, 0) + 1
            if len(evidence) >= self.max_evidence:
                break
        self.sources.assert_current(item.version_id for item in evidence)
        return evidence
