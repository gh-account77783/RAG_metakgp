"""Shared in-memory application fixtures; never real services or model calls."""
from pathlib import Path

from graphmind.config import Settings
from graphmind.domain import Outcome, ProviderAnswer
from graphmind.embeddings import HashEmbedding
from graphmind.errors import DependencyUnavailableError
from graphmind.providers import ExtractiveProvider
from graphmind.service import GraphMindApplication
from graphmind.storage import MemoryGraphStore, MemoryVectorStore


class FixtureProvider(ExtractiveProvider):
    def answer(self, question, evidence):
        for item in evidence:
            if "CERULEAN" in item.excerpt or "AMBER" in item.excerpt:
                word = "CERULEAN" if "CERULEAN" in item.excerpt else "AMBER"
                return ProviderAnswer(Outcome.ANSWER, word, (item.citation_id,))
        return ProviderAnswer(
            Outcome.INSUFFICIENT_EVIDENCE,
            "I don't know based on the provided knowledge set.",
        )


class BadCitationProvider(ExtractiveProvider):
    def answer(self, question, evidence):
        return ProviderAnswer(Outcome.ANSWER, "unsupported", ("not-retrieved",))


class FailingVectorStore(MemoryVectorStore):
    def search(self, installation_id, query, limit, **kwargs):
        raise DependencyUnavailableError("vector unavailable")


class WrongIdsVectorStore(MemoryVectorStore):
    def chunk_ids_for_version(self, installation_id, version_id):
        ids = super().chunk_ids_for_version(installation_id, version_id)
        return {f"wrong-{chunk_id}" for chunk_id in ids}


def make_settings(root: Path, import_root: Path | None = None, **overrides) -> Settings:
    values = {
        "data_dir": root / "runtime Ω with spaces",
        "allowed_import_roots": (import_root or root,),
        "chunk_size": 180,
        "chunk_overlap": 20,
        "embedding_dimension": 64,
        "job_lease_seconds": 10,
    }
    values.update(overrides)
    return Settings.load(
        dotenv_path=root / "missing.env",
        environ={},
        explicit=values,
    )


def make_app(root: Path, *, provider=None, failure_injector=None, settings=None):
    selected = settings or make_settings(root)
    embedding = HashEmbedding(selected.embedding_dimension)
    vector = MemoryVectorStore(embedding)
    graph = MemoryGraphStore()
    app = GraphMindApplication(
        selected,
        vector=vector,
        graph=graph,
        provider=provider or FixtureProvider(),
        failure_injector=failure_injector,
    )
    app.retrieval.seed_limit = 1
    return app


def import_and_run(app: GraphMindApplication, path: Path):
    submission = app.ingestion.prepare_txt_import(path)
    if submission.job.status.value == "failed":
        app.metadata.retry_job(submission.job.job_id)
    if submission.job.status.value != "succeeded":
        app.jobs.run_once()
    return submission
