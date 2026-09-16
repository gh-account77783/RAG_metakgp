from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from graphmind.config import Settings
from graphmind.domain import Chunk, Document, Evidence, Outcome, ProviderAnswer, QueryResult, Version
from graphmind.embeddings import HashEmbedding, OllamaEmbedding
from graphmind.errors import (
    ConfigurationError,
    EmbeddingDimensionError,
    EmbeddingModelNotFoundError,
    EmbeddingResponseError,
    EmbeddingUnavailableError,
    ModelNotFoundError,
    ProviderAuthenticationError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from graphmind.evaluation import EvaluationRunner, FrozenEvaluationDataset, MaterializedEvaluation
from graphmind.http_client import HttpStatusError, HttpTimeoutError, HttpUnavailableError
from graphmind.providers import OllamaProvider
from graphmind.retrieval import AnswerService
from graphmind.service import GraphMindApplication
from graphmind.storage import MemoryGraphStore, MemoryVectorStore


REPOSITORY = Path(__file__).resolve().parents[1]
FIXTURE = REPOSITORY / "tests" / "fixtures" / "evaluation" / "metakgp_50.json"
SOURCE = REPOSITORY / "Crawler" / "cleaned_wiki.jsonl"


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request_json(self, method, url, *, headers, payload, timeout):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "payload": payload,
                "timeout": timeout,
            }
        )
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class KeywordProvider:
    def answer(self, question, evidence):
        item = evidence[0]
        return ProviderAnswer(Outcome.ANSWER, "alpha", (item.citation_id,))


def make_settings(root: Path, **overrides) -> Settings:
    values = {
        "data_dir": root / "runtime",
        "allowed_import_roots": (root,),
        "embedding_provider": "hash",
        "embedding_dimension": 64,
        "answer_mode": "local",
        "answer_model": "gemma4:e2b",
        "ollama_base_url": "http://127.0.0.1:11434",
        "max_concurrent_queries": 1,
        "query_queue_timeout_seconds": 0,
    }
    values.update(overrides)
    return Settings.load(
        dotenv_path=root / "missing.env",
        environ={},
        explicit=values,
    )


class P4ConfigurationTests(unittest.TestCase):
    def test_bge_m3_local_configuration_and_secret_rules(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = make_settings(
                root,
                embedding_provider="ollama",
                embedding_dimension=1024,
                embedding_revision="auto",
                embedding_precision="fp8",
            )
            settings.validate(require_provider_secret=True)
            self.assertEqual(settings.embedding_model_id, "BAAI/bge-m3")
            with self.assertRaisesRegex(ConfigurationError, "loopback"):
                make_settings(root, ollama_base_url="https://models.example", answer_mode="local")
            hosted = make_settings(
                root,
                answer_mode="hosted",
                ollama_base_url="https://models.example",
            )
            with self.assertRaisesRegex(ConfigurationError, "OLLAMA_API_KEY"):
                hosted.validate(require_provider_secret=True)

    def test_invalid_embedding_identity_and_query_budgets_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ConfigurationError, "embedding_dimension=1024"):
                make_settings(root, embedding_provider="ollama", embedding_dimension=64)
            with self.assertRaisesRegex(ConfigurationError, "full SHA-256"):
                make_settings(root, embedding_revision="short", embedding_precision="fp16")
            with self.assertRaisesRegex(ConfigurationError, "retrieval_max_evidence"):
                make_settings(root, retrieval_seed_limit=5, retrieval_max_evidence=4)
            with self.assertRaisesRegex(ConfigurationError, "max_queued_queries"):
                make_settings(root, max_queued_queries=-1)


class OllamaEmbeddingTests(unittest.TestCase):
    def test_identity_batching_dimension_and_no_implicit_fp8_claim(self):
        digest = "a" * 64
        transport = FakeTransport(
            [
                {
                    "models": [
                        {
                            "name": "bge-m3:latest",
                            "digest": digest,
                            "details": {"quantization_level": "F16"},
                        }
                    ]
                },
                {"embeddings": [[1, 0, 0, 0], [0, 1, 0, 0]]},
                {"embeddings": [[0, 0, 1, 0]]},
            ]
        )
        embedding = OllamaEmbedding(
            "http://127.0.0.1:11434",
            "bge-m3",
            "BAAI/bge-m3",
            dimension=4,
            batch_size=2,
            transport=transport,
        )
        self.assertIn(f"@{digest}", embedding.fingerprint)
        self.assertIn("precision=fp16", embedding.fingerprint)
        self.assertEqual(len(embedding.embed_many(["a", "b", "c"])), 3)
        self.assertEqual([call["method"] for call in transport.calls], ["GET", "POST", "POST"])
        self.assertNotIn("Authorization", transport.calls[0]["headers"])

        mismatch = OllamaEmbedding(
            "http://127.0.0.1:11434",
            "bge-m3",
            "BAAI/bge-m3",
            dimension=4,
            precision="fp8",
            transport=FakeTransport(
                [
                    {
                        "models": [
                            {
                                "name": "bge-m3:latest",
                                "digest": digest,
                                "details": {"quantization_level": "F16"},
                            }
                        ]
                    }
                ]
            ),
        )
        with self.assertRaises(EmbeddingResponseError):
            _ = mismatch.fingerprint

    def test_invalid_embedding_dimensions_fail_closed(self):
        embedding = OllamaEmbedding(
            "http://127.0.0.1:11434",
            "bge-m3",
            "BAAI/bge-m3",
            dimension=4,
            revision="b" * 64,
            precision="fp16",
            transport=FakeTransport([{"embeddings": [[1, 2]]}]),
        )
        with self.assertRaises(EmbeddingDimensionError):
            embedding.embed("text")

    def test_missing_and_unavailable_embedding_models_are_typed(self):
        for transport_error, expected in (
            (HttpStatusError(404), EmbeddingModelNotFoundError),
            (HttpUnavailableError(), EmbeddingUnavailableError),
        ):
            with self.subTest(expected=expected.__name__), self.assertRaises(expected):
                OllamaEmbedding(
                    "http://127.0.0.1:11434",
                    "bge-m3",
                    "BAAI/bge-m3",
                    dimension=1024,
                    transport=FakeTransport([transport_error]),
                ).identity()


class OllamaProviderTests(unittest.TestCase):
    @staticmethod
    def evidence():
        return [
            Evidence(
                citation_id="citation",
                document_id="document",
                version_id="version",
                display_name="source.md",
                locator="line 1",
                excerpt="The code is CERULEAN.",
                via="graph",
                score=0.5,
            )
        ]

    def test_local_and_hosted_modes_have_explicit_credential_behavior(self):
        payload = {
            "message": {
                "content": json.dumps(
                    {
                        "outcome": "answer",
                        "answer": "CERULEAN",
                        "citation_ids": ["citation"],
                    }
                )
            }
        }
        transport = FakeTransport([payload])
        provider = OllamaProvider(
            "http://127.0.0.1:11434",
            "",
            "gemma4:e2b",
            mode="local",
            max_retries=0,
            transport=transport,
        )
        result = provider.answer("What is the code?", self.evidence())
        self.assertEqual(result.answer, "CERULEAN")
        self.assertNotIn("Authorization", transport.calls[0]["headers"])
        local_request = transport.calls[0]["payload"]
        self.assertIsInstance(local_request["format"], dict)
        self.assertEqual(
            local_request["format"]["properties"]["outcome"]["enum"],
            ["answer", "insufficient_evidence"],
        )
        self.assertEqual(
            local_request["format"]["required"],
            ["outcome", "answer", "citation_ids"],
        )
        self.assertFalse(local_request["format"]["additionalProperties"])
        system_prompt = local_request["messages"][0]["content"]
        self.assertIn('"outcome":"answer"', system_prompt)
        self.assertIn('"outcome":"insufficient_evidence"', system_prompt)
        self.assertIn("never put the factual answer in outcome", system_prompt)

        hosted_transport = FakeTransport([payload])
        hosted = OllamaProvider(
            "https://models.example",
            "secret",
            "hosted-model",
            mode="hosted",
            max_retries=0,
            transport=hosted_transport,
        )
        self.assertEqual(
            hosted.answer("What is the code?", self.evidence()).answer,
            "CERULEAN",
        )
        self.assertEqual(hosted_transport.calls[0]["payload"]["format"], "json")
        self.assertEqual(
            hosted_transport.calls[0]["headers"]["Authorization"], "Bearer secret"
        )

        with self.assertRaises(ProviderAuthenticationError):
            OllamaProvider(
                "https://models.example",
                "",
                "hosted-model",
                mode="hosted",
                transport=FakeTransport([]),
            ).answer("question", self.evidence())

    def test_retry_and_typed_http_failures(self):
        success = {
            "message": {
                "content": '{"outcome":"answer","answer":"x","citation_ids":["citation"]}'
            }
        }
        transport = FakeTransport([HttpStatusError(429), success])
        provider = OllamaProvider(
            "https://models.example",
            "secret",
            "model",
            max_retries=1,
            transport=transport,
            sleep=lambda _: None,
        )
        self.assertEqual(provider.answer("question", self.evidence()).answer, "x")
        self.assertEqual(provider.call_count, 2)

        failures = (
            (HttpStatusError(401), ProviderAuthenticationError),
            (HttpStatusError(404), ModelNotFoundError),
            (HttpStatusError(429), ProviderRateLimitError),
            (HttpStatusError(500), ProviderUnavailableError),
            (HttpTimeoutError(), ProviderTimeoutError),
            (HttpUnavailableError(), ProviderUnavailableError),
        )
        for transport_error, expected in failures:
            with self.subTest(expected=expected.__name__), self.assertRaises(expected):
                OllamaProvider(
                    "https://models.example",
                    "secret",
                    "model",
                    max_retries=0,
                    transport=FakeTransport([transport_error]),
                ).answer("question", self.evidence())

        with self.assertRaises(ProviderResponseError):
            OllamaProvider(
                "https://models.example",
                "secret",
                "model",
                max_retries=0,
                transport=FakeTransport([{"unexpected": "envelope"}]),
            ).answer("question", self.evidence())

    def test_context_is_bounded_and_failure_calls_are_reported(self):
        payload = {
            "message": {
                "content": '{"outcome":"answer","answer":"x","citation_ids":["citation"]}'
            }
        }
        transport = FakeTransport([payload])
        provider = OllamaProvider(
            "https://models.example",
            "secret",
            "model",
            max_retries=0,
            max_context_chars=1000,
            transport=transport,
        )
        provider.answer(
            "question",
            [
                Evidence(
                    "citation",
                    "document",
                    "version",
                    "source.md",
                    "line 1",
                    "x" * 1200,
                    "vector",
                    1,
                ),
                Evidence(
                    "unused",
                    "document",
                    "version",
                    "source.md",
                    "line 2",
                    "y",
                    "vector",
                    0.5,
                ),
            ],
        )
        supplied = json.loads(transport.calls[0]["payload"]["messages"][1]["content"])
        self.assertEqual(len(supplied["evidence"]), 1)
        self.assertEqual(len(supplied["evidence"][0]["text"]), 1000)

        failing = OllamaProvider(
            "https://models.example",
            "secret",
            "model",
            max_retries=1,
            retry_backoff=0,
            transport=FakeTransport([HttpStatusError(429), HttpStatusError(429)]),
            sleep=lambda _: None,
        )

        class Retrieval:
            def retrieve(self, question):
                return self_evidence

        self_evidence = tuple(self.evidence())

        class Metadata:
            def active_version_ids(self):
                return {"version"}

        result = AnswerService(Metadata(), Retrieval(), failing).query("question")
        self.assertEqual(result.outcome, Outcome.ERROR)
        self.assertEqual(result.verification["provider_calls"], 2)


class GraphAndCompatibilityTests(unittest.TestCase):
    def test_real_chroma_reopen_rejects_an_incompatible_fingerprint(self):
        with tempfile.TemporaryDirectory() as temporary:
            code = """
import sys
from graphmind.embeddings import HashEmbedding
from graphmind.errors import EmbeddingMismatchError
from graphmind.storage import ChromaVectorStore

first = ChromaVectorStore(sys.argv[1], "gm_p4_fingerprint_check", HashEmbedding(64))
assert first.readiness().available
first.close()
second = ChromaVectorStore(sys.argv[1], "gm_p4_fingerprint_check", HashEmbedding(128))
status = second.readiness()
assert not status.available and "fingerprint" in status.detail
try:
    second.chunk_ids_for_version("installation", "version")
except EmbeddingMismatchError:
    pass
else:
    raise AssertionError("typed mismatch was not preserved")
finally:
    second.close()
"""
            completed = subprocess.run(
                [sys.executable, "-B", "-c", code, temporary],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_document_scope_expands_references_from_a_sibling_chunk(self):
        graph = MemoryGraphStore()
        source = Document("source", "default", "source.md", "source.md", "text/markdown", "s", "now")
        target = Document("target", "default", "target.md", "target.md", "text/markdown", "t", "now")
        source_version = Version("sv", "source", "h", "e", "c", "f", "p", 1, 2, "d", "ready", "now")
        target_version = Version("tv", "target", "h", "e", "c", "f", "p", 1, 1, "d", "ready", "now")
        chunks = [
            Chunk("reference", "sv", "source", 0, "link", "line 1", 0, 4, ("target.md",)),
            Chunk("seed", "sv", "source", 1, "query terms", "line 2", 5, 16),
        ]
        graph.upsert_version("install", source, source_version, chunks)
        graph.upsert_version(
            "install",
            target,
            target_version,
            [Chunk("answer", "tv", "target", 0, "fact", "line 1", 0, 4)],
        )
        self.assertEqual(graph.expand("install", ["seed"], 4, "chunk"), [])
        self.assertEqual(graph.expand("install", ["seed"], 4, "document"), ["answer"])

    def test_active_embedding_mismatch_fails_query_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.txt"
            source.write_text("alpha", encoding="utf-8")
            first_embedding = HashEmbedding(64)
            with GraphMindApplication(
                make_settings(root),
                vector=MemoryVectorStore(first_embedding),
                graph=MemoryGraphStore(),
                provider=KeywordProvider(),
            ) as app:
                submission = app.ingestion.prepare_import(source)
                app.jobs.run_once()
                self.assertEqual(submission.version.embedding_fingerprint, first_embedding.fingerprint)
            second_embedding = HashEmbedding(128)
            with GraphMindApplication(
                make_settings(root, embedding_dimension=128),
                vector=MemoryVectorStore(second_embedding),
                graph=MemoryGraphStore(),
                provider=KeywordProvider(),
            ) as app:
                result = app.answers.query("alpha")
                self.assertEqual(result.outcome, Outcome.ERROR)
                self.assertEqual(
                    result.verification["error_code"], "embedding_fingerprint_mismatch"
                )


class EvaluationTests(unittest.TestCase):
    def test_materialization_is_deterministic_and_contains_verified_graph_links(self):
        dataset = FrozenEvaluationDataset(FIXTURE, SOURCE)
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            left = dataset.materialize(Path(first))
            right = dataset.materialize(Path(second))
            self.assertEqual(len(left.document_paths), 49)
            self.assertEqual(left.materialization_sha256, right.materialization_sha256)
            left_bytes = {path.name: path.read_bytes() for path in left.document_paths}
            right_bytes = {path.name: path.read_bytes() for path in right.document_paths}
            self.assertEqual(left_bytes, right_bytes)
            source_name = left.url_to_filename[
                "https://wiki.metakgp.org/w/MA61027:_Cryptography_And_Network_Security"
            ]
            target_name = left.url_to_filename[
                "https://wiki.metakgp.org/w/Sourav_Mukhopadhyay"
            ]
            self.assertIn(f"[[{target_name}]]", (left.root / source_name).read_text("utf-8"))

    def test_case_scoring_separates_retrieval_graph_and_citations(self):
        source_url = "https://example/source"
        target_url = "https://example/target"
        materialized = MaterializedEvaluation(
            root=Path("."),
            manifest_path=Path("manifest.json"),
            dataset_id="fixture",
            materialization_sha256="hash",
            url_to_filename={source_url: "source.md", target_url: "target.md"},
        )
        evidence = (
            Evidence("s", "sd", "sv", "source.md", "line 1", "course link", "vector", 1),
            Evidence("t", "td", "tv", "target.md", "line 1", "joined 2009", "vector", 0.9),
        )
        result = QueryResult(
            Outcome.ANSWER,
            "The instructor joined in 2009.",
            evidence,
            "request",
            {"citations_valid": True},
        )

        class Answers:
            def query_with_evidence(self, question):
                return result, evidence

        class Dataset:
            dataset_id = "fixture"
            cases = []

        runner = EvaluationRunner(
            Dataset(),
            materialized,
            Answers(),
            embedding_identity={},
            provider_identity={},
            label="test",
        )
        scored = runner.evaluate_case(
            {
                "id": "G",
                "category": "graph",
                "question": "When did the linked instructor join?",
                "expected_outcome": "answer",
                "reference_answer": "2009",
                "required_answer_terms": ["2009"],
                "evidence": [{"url": source_url}, {"url": target_url}],
                "graph_edges": [{"from_url": source_url, "to_url": target_url}],
            }
        )
        self.assertTrue(scored["citation_pass"])
        self.assertFalse(scored["retrieval_pass"])
        self.assertFalse(scored["graph_target_recalled"])

    def test_abstention_retrieval_is_not_scored_as_a_retrieval_pass(self):
        materialized = MaterializedEvaluation(
            root=Path("."),
            manifest_path=Path("manifest.json"),
            dataset_id="fixture",
            materialization_sha256="hash",
            url_to_filename={},
        )
        result = QueryResult(
            Outcome.INSUFFICIENT_EVIDENCE,
            "I don't know.",
            (),
            "request",
            {"citations_valid": True, "duration_ms": 1, "provider_calls": 0},
        )

        class Answers:
            def query_with_evidence(self, question):
                return result, ()

        class Dataset:
            dataset_id = "fixture"
            cases = []

        scored = EvaluationRunner(
            Dataset(),
            materialized,
            Answers(),
            embedding_identity={},
            provider_identity={},
            label="test",
        ).evaluate_case(
            {
                "id": "A",
                "category": "abstain",
                "question": "Unknown?",
                "expected_outcome": "abstain",
                "reference_answer": "Not present.",
            }
        )
        self.assertIsNone(scored["retrieval_pass"])
        self.assertTrue(scored["answer_pass"])


class QueryBudgetTests(unittest.TestCase):
    def test_question_budget_returns_a_typed_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            embedding = HashEmbedding(64)
            with GraphMindApplication(
                make_settings(root, max_question_chars=4),
                vector=MemoryVectorStore(embedding),
                graph=MemoryGraphStore(),
                provider=KeywordProvider(),
            ) as app:
                result = app.answers.query("too long")
                self.assertEqual(result.outcome, Outcome.ERROR)
                self.assertEqual(result.verification["error_code"], "query_budget_exceeded")

    def test_query_capacity_stays_held_until_the_provider_finishes(self):
        entered = threading.Event()
        release = threading.Event()
        evidence = Evidence(
            "citation", "document", "version", "source.md", "line 1", "alpha", "vector", 1
        )

        class Retrieval:
            def retrieve(self, question):
                return [evidence]

        class Metadata:
            def active_version_ids(self):
                return {"version"}

        class BlockingProvider:
            def answer(self, question, supplied):
                entered.set()
                release.wait(timeout=5)
                return ProviderAnswer(Outcome.ANSWER, "alpha", ("citation",))

        service = AnswerService(
            Metadata(), Retrieval(), BlockingProvider(), max_concurrent_queries=1, queue_timeout=0
        )
        holder = {}

        def first_query():
            holder["result"] = service.query("first")

        thread = threading.Thread(target=first_query)
        thread.start()
        self.assertTrue(entered.wait(timeout=2))
        overloaded = service.query("second")
        self.assertEqual(overloaded.outcome, Outcome.ERROR)
        self.assertEqual(overloaded.verification["error_code"], "query_capacity_exceeded")
        release.set()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(holder["result"].outcome, Outcome.ANSWER)

    def test_waiting_queue_is_count_bounded(self):
        entered = threading.Event()
        release = threading.Event()
        evidence = Evidence(
            "citation", "document", "version", "source.md", "line 1", "alpha", "vector", 1
        )

        class Retrieval:
            def retrieve(self, question):
                return [evidence]

        class Metadata:
            def active_version_ids(self):
                return {"version"}

        class BlockingProvider:
            def answer(self, question, supplied):
                entered.set()
                release.wait(timeout=5)
                return ProviderAnswer(Outcome.ANSWER, "alpha", ("citation",))

        service = AnswerService(
            Metadata(),
            Retrieval(),
            BlockingProvider(),
            max_concurrent_queries=1,
            max_queued_queries=1,
            queue_timeout=5,
        )
        results = []
        first = threading.Thread(target=lambda: results.append(service.query("first")))
        second = threading.Thread(target=lambda: results.append(service.query("second")))
        first.start()
        self.assertTrue(entered.wait(timeout=2))
        second.start()
        for _ in range(100):
            with service._waiters_lock:
                if service._waiters == 1:
                    break
            threading.Event().wait(0.01)
        else:
            self.fail("second query did not enter the bounded queue")
        rejected = service.query("third")
        self.assertEqual(rejected.outcome, Outcome.ERROR)
        self.assertEqual(rejected.verification["error_code"], "query_capacity_exceeded")
        release.set()
        first.join(timeout=2)
        second.join(timeout=2)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(len(results), 2)
        self.assertTrue(all(item.outcome is Outcome.ANSWER for item in results))

    def test_cancellation_discards_a_running_provider_result_before_releasing_capacity(self):
        entered = threading.Event()
        release = threading.Event()
        cancelled = threading.Event()
        evidence = Evidence(
            "citation", "document", "version", "source.md", "line 1", "alpha", "vector", 1
        )

        class Retrieval:
            def retrieve(self, question):
                return [evidence]

        class Metadata:
            def active_version_ids(self):
                return {"version"}

        class BlockingProvider:
            def answer(self, question, supplied):
                entered.set()
                release.wait(timeout=5)
                return ProviderAnswer(Outcome.ANSWER, "alpha", ("citation",))

        service = AnswerService(
            Metadata(),
            Retrieval(),
            BlockingProvider(),
            max_concurrent_queries=1,
            max_queued_queries=0,
            queue_timeout=0,
        )
        holder = {}
        thread = threading.Thread(
            target=lambda: holder.setdefault(
                "result", service.query("first", cancel_event=cancelled)
            )
        )
        thread.start()
        self.assertTrue(entered.wait(timeout=2))
        cancelled.set()
        still_full = service.query("second")
        self.assertEqual(still_full.verification["error_code"], "query_capacity_exceeded")
        release.set()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(holder["result"].outcome, Outcome.ERROR)
        self.assertEqual(holder["result"].verification["error_code"], "query_cancelled")


if __name__ == "__main__":
    unittest.main()
