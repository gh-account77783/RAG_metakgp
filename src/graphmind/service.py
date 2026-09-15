"""Composition root for CLI, tests, and future HTTP/MCP adapters."""

from __future__ import annotations

from .config import Settings
from .domain import AdapterStatus
from .embeddings import EmbeddingProvider, HashEmbedding, OllamaEmbedding
from .files import FileStore, PrivateFileStore
from .ingestion import IngestionService
from .jobs import DurableJobExecutor
from .metadata import MetadataStore
from .parsing import IsolatedExtractor
from .providers import AnswerProvider, OllamaProvider
from .retrieval import AnswerService, RetrievalService
from .storage import ChromaVectorStore, GraphStore, Neo4jGraphStore, VectorStore


def build_embedding(settings: Settings) -> EmbeddingProvider:
    if settings.embedding_provider == "ollama":
        return OllamaEmbedding(
            settings.embedding_base_url,
            settings.embedding_model,
            settings.embedding_model_id,
            dimension=settings.embedding_dimension,
            revision=settings.embedding_revision,
            precision=settings.embedding_precision,
            api_key=settings.embedding_api_key,
            timeout=float(settings.embedding_timeout_seconds),
            batch_size=settings.embedding_batch_size,
        )
    return HashEmbedding(settings.embedding_dimension)


class GraphMindApplication:
    def __init__(
        self,
        settings: Settings,
        *,
        vector: VectorStore | None = None,
        graph: GraphStore | None = None,
        provider: AnswerProvider | None = None,
        metadata: MetadataStore | None = None,
        files: FileStore | None = None,
        extractor: IsolatedExtractor | None = None,
        embedding: EmbeddingProvider | None = None,
        failure_injector=None,
    ) -> None:
        self.settings = settings
        settings.ensure_private_paths()
        self.metadata = metadata or MetadataStore(settings.sqlite_path)
        self.metadata.migrate()
        self.installation_id = self.metadata.installation_id()
        if embedding is not None:
            self.embedding = embedding
        elif vector is not None and hasattr(vector, "embedding"):
            self.embedding = vector.embedding
        else:
            self.embedding = build_embedding(settings)
        self.files = files or PrivateFileStore(settings.data_dir, settings.files_dir)
        self.extractor = extractor or IsolatedExtractor(settings)
        self.extractor.cleanup_abandoned()
        self.vector = vector or ChromaVectorStore(
            str(settings.chroma_dir), settings.collection_name, self.embedding
        )
        self.graph = graph or Neo4jGraphStore(
            settings.neo4j_uri,
            settings.neo4j_username,
            settings.neo4j_password,
            settings.neo4j_database,
        )
        self.ingestion = IngestionService(
            settings,
            self.metadata,
            self.vector,
            self.graph,
            self.installation_id,
            self.embedding.fingerprint,
            self.files,
            self.extractor,
            failure_injector=failure_injector,
        )
        self.jobs = DurableJobExecutor(
            self.metadata,
            self.ingestion.process_job,
            lease_seconds=settings.job_lease_seconds,
        )
        self.retrieval = RetrievalService(
            self.metadata,
            self.vector,
            self.graph,
            self.installation_id,
            seed_limit=settings.retrieval_seed_limit,
            graph_limit=settings.retrieval_graph_limit,
            graph_scope=settings.retrieval_graph_scope,
            max_evidence=settings.retrieval_max_evidence,
            min_vector_score=settings.retrieval_min_vector_score,
            max_question_chars=settings.max_question_chars,
        )
        selected_provider = provider or OllamaProvider(
            settings.ollama_base_url,
            settings.ollama_api_key,
            settings.answer_model,
            mode=settings.answer_mode,
            timeout=float(settings.answer_timeout_seconds),
            max_retries=settings.answer_max_retries,
            max_context_chars=settings.answer_max_context_chars,
            max_output_tokens=settings.answer_max_output_tokens,
        )
        self.answers = AnswerService(
            self.metadata,
            self.retrieval,
            selected_provider,
            max_concurrent_queries=settings.max_concurrent_queries,
            max_queued_queries=settings.max_queued_queries,
            queue_timeout=settings.query_queue_timeout_seconds,
        )

    def readiness(self):
        statuses = [
            self.files.readiness(),
            self.embedding.readiness(),
            self.vector.readiness(),
            self.graph.readiness(),
        ]
        statuses.append(self.audit_manifest())
        return tuple(statuses)

    def audit_manifest(self) -> AdapterStatus:
        try:
            for document in self.metadata.list_documents():
                if document.deleted_at is not None or document.active_version_id is None:
                    continue
                version = self.metadata.version(document.active_version_id)
                if version is None:
                    return AdapterStatus("active-manifest", False, "active version metadata is missing")
                if version.status.value != "ready":
                    return AdapterStatus("active-manifest", False, "active version is not ready")
                chunks = self.metadata.all_chunks_for_version(version.version_id)
                expected_ids = {chunk.chunk_id for chunk in chunks}
                stored_ids = (
                    expected_ids,
                    self.vector.chunk_ids_for_version(self.installation_id, version.version_id),
                    self.graph.chunk_ids_for_version(self.installation_id, version.version_id),
                )
                if (
                    len(expected_ids) != version.expected_chunk_count
                    or any(item != expected_ids for item in stored_ids)
                ):
                    return AdapterStatus(
                        "active-manifest",
                        False,
                        f"version {version.version_id} expected {version.expected_chunk_count} exact chunk IDs; stores differ",
                    )
            return AdapterStatus("active-manifest", True, "ready")
        except Exception as exc:
            return AdapterStatus("active-manifest", False, f"unavailable: {type(exc).__name__}")

    def close(self) -> None:
        self.graph.close()
        self.vector.close()

    def __enter__(self) -> "GraphMindApplication":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()
