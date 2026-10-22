"""Composition root for CLI, tests, and future HTTP/MCP adapters."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from .config import Settings
from .rag.components.contracts import AdapterStatus
from .rag.components.embeddings import EmbeddingProvider, HashEmbedding, OllamaEmbedding
from .files import FileStore, PrivateFileStore
from .ingestion import IngestionService
from .jobs import DurableJobExecutor
from .metadata import MetadataStore
from .sources import SourceService
from .rag.components.indexes import IndexRepository
from .rag.components.artifacts import DerivedArtifactStore
from .rag.components.local_inference import LocalCrossEncoder, LocalInference
from .rag.components.router import EngineRouter
from .rag.tree_navigation.engine import TreeNavigation
from .rag.tree_navigation.pageindex_adapter import PageIndexAdapter
from .rag.graphmind.hybrid import HybridGraphMind
from .parsing import IsolatedExtractor
from .providers import AnswerProvider, OllamaProvider
from .rag.components.pipeline import AnswerService
from .rag.graphmind.engine import RetrievalService
from .rag.components.vector_store import ChromaVectorStore, VectorStore
from .rag.graphmind.reference_graph import GraphStore, Neo4jGraphStore


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
        hybrid_vector: VectorStore | None = None,
        reranker=None,
        visual_adapter=None,
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
        self.sources = SourceService(self.metadata, self.files, max_file_bytes=settings.max_file_bytes)
        self.indexes = IndexRepository(self.metadata, self.sources)
        self.artifacts = DerivedArtifactStore(settings.data_dir, self.indexes)
        self.inference = LocalInference(settings.data_dir)
        self.use_model_admission = settings.retrieval_profile == "hybrid" or any(
            engine != "graphmind" for engine in settings.enabled_engines)
        if (self.use_model_admission and isinstance(self.embedding, OllamaEmbedding)
                and urlparse(settings.embedding_base_url).hostname in {"127.0.0.1", "localhost", "::1"}):
            self.embedding.inference_lock = self.inference.lock_path
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
        self.hybrid = None
        if settings.retrieval_profile == "hybrid" and "graphmind" in settings.enabled_engines:
            self.hybrid = HybridGraphMind(self.indexes, self.sources, self.artifacts,
                hybrid_vector or ChromaVectorStore(str(settings.chroma_dir / "hybrid"),
                                                   settings.collection_name + "_hybrid_v1", self.embedding),
                self.graph, self.installation_id,
                reranker or LocalCrossEncoder(Path(settings.reranker_dir), self.inference),
                max_evidence=settings.retrieval_max_evidence, max_question_chars=settings.max_question_chars,
                min_vector_score=settings.retrieval_min_vector_score,
                seed_limit=settings.retrieval_seed_limit, graph_limit=settings.retrieval_graph_limit)
        self.ingestion = IngestionService(
            settings,
            self.metadata,
            self.vector,
            self.graph,
            self.installation_id,
            lambda: self.embedding.fingerprint,
            self.files,
            self.extractor,
            indexes=self.indexes,
            backend=self.hybrid,
            artifacts=self.artifacts,
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
            indexes=self.indexes,
            sources=self.sources,
        )
        if self.hybrid is not None:
            self.retrieval = self.hybrid
        self.router = EngineRouter({"graphmind": self.retrieval} if "graphmind" in settings.enabled_engines else {})
        selected_provider = provider or OllamaProvider(
            settings.ollama_base_url,
            settings.ollama_api_key,
            settings.answer_model,
            mode=settings.answer_mode,
            timeout=float(settings.answer_timeout_seconds),
            max_retries=settings.answer_max_retries,
            max_context_chars=settings.answer_max_context_chars,
            max_output_tokens=settings.answer_max_output_tokens,
            local_context_tokens=settings.answer_local_context_tokens,
            inference_lock=self.inference.lock_path if self.use_model_admission else None,
        )
        if self.use_model_admission and isinstance(selected_provider, OllamaProvider):
            selected_provider.inference_lock = self.inference.lock_path
        self.tree = None
        if "tree_navigation" in settings.enabled_engines:
            self.tree = TreeNavigation(self.indexes, self.sources, self.artifacts,
                PageIndexAdapter(self.inference, settings.staging_dir), selected_provider,
                max_pdf_pages=settings.max_pdf_pages, max_question_chars=settings.max_question_chars,
                max_context_chars=settings.answer_max_context_chars)
            self.ingestion.backends["tree_navigation"] = self.tree
            self.router.retrievers["tree_navigation"] = self.tree
        self.visual = None
        if "multimodal" in settings.enabled_engines:
            from .rag.multimodal.adapter import ColQwenAdapter
            from .rag.multimodal.engine import VisualRetrieval
            self.visual = VisualRetrieval(self.indexes, self.sources, self.artifacts,
                visual_adapter or ColQwenAdapter(Path(settings.visual_model_dir), self.inference, settings.staging_dir,
                                                 build_timeout=settings.visual_build_timeout_seconds),
                self.inference, settings.staging_dir, max_question_chars=settings.max_question_chars)
            self.ingestion.backends["multimodal"] = self.visual
            self.router.retrievers["multimodal"] = self.visual
        self.answers = AnswerService(
            self.metadata,
            self.router,
            selected_provider,
            max_concurrent_queries=settings.max_concurrent_queries,
            max_queued_queries=settings.max_queued_queries,
            queue_timeout=settings.query_queue_timeout_seconds,
            sources=self.sources,
        )

    def readiness(self):
        """Host-local doctor includes the expensive manifest consistency audit."""
        return (*self._storage_readiness(), self.audit_manifest())

    def reader_readiness(self):
        """A missing optional engine does not disable another ready engine."""
        available = False
        for engine_id in ("tree_navigation", "multimodal", "graphmind"):
            statuses = self.engine_readiness((engine_id,)).get(engine_id, ())
            if statuses and all(item.available for item in statuses):
                available = True
                break
        return (self.files.readiness(),
                AdapterStatus("rag-engines", available, "available" if available else "unavailable"),
                self.answers.provider.readiness())

    def engine_readiness(self, engine_ids=None):
        result = {}
        selected = set(self.settings.enabled_engines if engine_ids is None else engine_ids)
        if "graphmind" in selected and "graphmind" in self.settings.enabled_engines:
            statuses = [self.embedding.readiness(),
                        (self.hybrid.vector if self.hybrid is not None else self.vector).readiness(),
                        self.graph.readiness()]
            if self.hybrid is not None and hasattr(self.hybrid.reranker, "readiness"):
                statuses.append(self.hybrid.reranker.readiness())
            result["graphmind"] = statuses
        if self.tree is not None and "tree_navigation" in selected:
            from importlib.metadata import PackageNotFoundError, version
            try:
                available = version("pageindex") == "0.4.4"
            except PackageNotFoundError:
                available = False
            result["tree_navigation"] = [AdapterStatus("tree-layout", available,
                "ready" if available else "dependency missing")]
        if self.visual is not None and "multimodal" in selected:
            try:
                self.visual.adapter.identity()
                available = True
            except Exception:
                available = False
            provider = self.answers.provider
            image_status = (provider.image_readiness() if hasattr(provider, "image_readiness") else
                            AdapterStatus("image-input-provider", False, "provider does not support images"))
            result["multimodal"] = [AdapterStatus("visual-model", available,
                "configured" if available else "model missing or invalid"), image_status]
        return result

    def _storage_readiness(self):
        return (self.files.readiness(), *(status for group in self.engine_readiness().values() for status in group))

    def audit_manifest(self) -> AdapterStatus:
        try:
            if self.tree is not None:
                for index in self.indexes.current("tree_navigation"):
                    self.artifacts.read(index)
            if self.visual is not None:
                for index in self.indexes.current("multimodal"):
                    self.artifacts.read(index)
            if self.hybrid is not None:
                self.hybrid.audit()
                return AdapterStatus("active-manifest", True, "ready")
            if "graphmind" not in self.settings.enabled_engines:
                return AdapterStatus("active-manifest", True, "ready")
            for document in self.metadata.list_documents():
                if document.deleted_at is not None or document.active_version_id is None:
                    continue
                version = self.metadata.version(document.active_version_id)
                if version is None:
                    return AdapterStatus("active-manifest", False, "active version metadata is missing")
                if version.status.value != "ready":
                    return AdapterStatus("active-manifest", False, "active version is not ready")
                if self.indexes.ready(document.document_id, "graphmind") is None:
                    continue  # Source may have been indexed for another configured engine.
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
        if self.hybrid is not None:
            self.hybrid.vector.close()
        self.graph.close()
        self.vector.close()

    def __enter__(self) -> "GraphMindApplication":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()
