from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from graphmind.config import Settings
from graphmind.domain import Outcome, ProviderAnswer, VersionStatus
from graphmind.embeddings import HashEmbedding
from graphmind.errors import (
    DependencyUnavailableError,
    ManifestUnavailableError,
    ProviderResponseError,
    PublicationError,
)
from graphmind.providers import ExtractiveProvider, OllamaProvider
from graphmind.retrieval import AnswerService, RetrievalService
from graphmind.service import GraphMindApplication
from graphmind.storage import MemoryGraphStore, MemoryVectorStore


class FixtureProvider:
    def answer(self, question, evidence):
        for item in evidence:
            if "CERULEAN" in item.excerpt or "AMBER" in item.excerpt:
                word = "CERULEAN" if "CERULEAN" in item.excerpt else "AMBER"
                return ProviderAnswer(Outcome.ANSWER, word, (item.citation_id,))
        return ProviderAnswer(
            Outcome.INSUFFICIENT_EVIDENCE,
            "I don't know based on the provided knowledge set.",
        )


class BadCitationProvider:
    def answer(self, question, evidence):
        return ProviderAnswer(Outcome.ANSWER, "unsupported", ("not-retrieved",))


class FailingVectorStore(MemoryVectorStore):
    def search(self, installation_id, query, limit):
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


class TxtValidationTests(unittest.TestCase):
    def test_rejects_empty_large_encoding_extension_and_disallowed_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            allowed = root / "allowed"
            outside = root / "outside"
            allowed.mkdir()
            outside.mkdir()
            settings = make_settings(root, allowed, max_file_bytes=8)
            with make_app(root, settings=settings) as app:
                empty = allowed / "empty.txt"
                empty.write_text("", encoding="utf-8")
                large = allowed / "large.txt"
                large.write_text("123456789", encoding="utf-8")
                invalid = allowed / "invalid.txt"
                invalid.write_bytes(b"\xff\xfe\x00")
                wrong = allowed / "wrong.md"
                wrong.write_text("text", encoding="utf-8")
                disallowed = outside / "outside.txt"
                disallowed.write_text("text", encoding="utf-8")
                cases = (empty, large, invalid, wrong, disallowed)
                for path in cases:
                    with self.subTest(path=path.name), self.assertRaises(Exception):
                        app.ingestion.prepare_txt_import(path)

    def test_deterministic_ids_locators_and_duplicate_import(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "repeat.txt"
            source.write_text("First line.\nSecond line.\nThird line.", encoding="utf-8")
            with make_app(root) as app:
                first = import_and_run(app, source)
                second = app.ingestion.prepare_txt_import(source)
                self.assertEqual(first.document.document_id, second.document.document_id)
                self.assertEqual(first.version.version_id, second.version.version_id)
                self.assertEqual(first.job.job_id, second.job.job_id)
                self.assertEqual(second.job.status.value, "succeeded")
                chunks = app.metadata.all_chunks_for_version(first.version.version_id)
                self.assertTrue(all(chunk.locator.startswith(("line ", "lines ")) for chunk in chunks))
                self.assertEqual(app.vector.count_version(app.installation_id, first.version.version_id), len(chunks))
                self.assertEqual(app.graph.count_version(app.installation_id, first.version.version_id), len(chunks))

    def test_private_original_preserves_source_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "original.txt"
            original = b"\xef\xbb\xbfCafe\xcc\x81\r\nSecond line."
            source.write_bytes(original)
            with make_app(root) as app:
                submission = app.ingestion.prepare_txt_import(source)
                stored = app.settings.data_dir / submission.version.private_file_ref
                self.assertEqual(stored.read_bytes(), original)
                self.assertEqual(submission.version.source_size, len(original))


class PublicationAndRetrievalTests(unittest.TestCase):
    def test_empty_installation_is_ready_and_missing_active_records_are_detected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "known.txt"
            source.write_text("Known active content.", encoding="utf-8")
            with make_app(root) as app:
                self.assertTrue(all(status.available for status in app.readiness()))
                submission = import_and_run(app, source)
                self.assertTrue(app.audit_manifest().available)
                key = (app.installation_id, app.metadata.all_chunks_for_version(submission.version.version_id)[0].chunk_id)
                del app.vector._items[key]
                audit = app.audit_manifest()
                self.assertFalse(audit.available)
                self.assertIn("expected", audit.detail)

    def test_first_import_failures_before_publication_stay_invisible_and_retry(self):
        for point in ("after_vector", "after_graph", "before_publish"):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "failure.txt"
                source.write_text("Failure-injection fixture.", encoding="utf-8")

                def inject(current: str, selected=point):
                    if current == selected:
                        raise PublicationError(f"injected {selected}")

                with make_app(root, failure_injector=inject) as app:
                    submission = app.ingestion.prepare_txt_import(source)
                    with self.assertRaises(PublicationError):
                        app.jobs.run_once()
                    self.assertIsNone(
                        app.metadata.document(submission.document.document_id).active_version_id
                    )
                    app.ingestion.failure_injector = None
                    app.metadata.retry_job(submission.job.job_id)
                    app.jobs.run_once()
                    self.assertEqual(
                        app.metadata.document(submission.document.document_id).active_version_id,
                        submission.version.version_id,
                    )

    def test_equal_counts_with_wrong_store_ids_do_not_publish(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "wrong-ids.txt"
            source.write_text("Exact chunk identities matter.", encoding="utf-8")
            settings = make_settings(root)
            vector = WrongIdsVectorStore(HashEmbedding(settings.embedding_dimension))
            with GraphMindApplication(
                settings,
                vector=vector,
                graph=MemoryGraphStore(),
                provider=FixtureProvider(),
            ) as app:
                submission = app.ingestion.prepare_txt_import(source)
                with self.assertRaises(PublicationError):
                    app.jobs.run_once()
                self.assertEqual(
                    app.vector.count_version(app.installation_id, submission.version.version_id),
                    submission.version.expected_chunk_count,
                )
                self.assertIsNone(
                    app.metadata.document(submission.document.document_id).active_version_id
                )

    def test_interrupted_job_reconciles_after_application_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = make_settings(root)
            source = root / "restart.txt"
            source.write_text("Restart-safe content.", encoding="utf-8")
            first = make_app(root, settings=settings)
            submission = first.ingestion.prepare_txt_import(source)
            self.assertTrue(first.metadata.acquire_writer("crashed", 30))
            first.metadata.claim_next_job("crashed", 30)
            with first.metadata.transaction() as connection:
                connection.execute(
                    "UPDATE jobs SET lease_expires_at = '2000-01-01T00:00:00+00:00' WHERE job_id = ?",
                    (submission.job.job_id,),
                )
                connection.execute(
                    "UPDATE writer_lock SET lease_expires_at = '2000-01-01T00:00:00+00:00' WHERE singleton = 1"
                )
            first.close()

            with make_app(root, settings=settings) as restarted:
                self.assertEqual(restarted.installation_id, first.installation_id)
                self.assertEqual(restarted.jobs.reconcile(), 1)
                restarted.jobs.run_once()
                self.assertEqual(
                    restarted.metadata.document(submission.document.document_id).active_version_id,
                    submission.version.version_id,
                )

    def test_graph_supplies_missing_active_evidence_and_citations(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            a = root / "A.txt"
            b = root / "B.txt"
            a.write_text(
                "The blue lantern protocol controls Zephyr authorization values. "
                "The answer record is maintained in [[B.txt]].",
                encoding="utf-8",
            )
            b.write_text("The requested authorization value is CERULEAN.", encoding="utf-8")
            with make_app(root) as app:
                import_and_run(app, a)
                b_submission = import_and_run(app, b)
                question = "Under the blue lantern protocol for Zephyr, what is the authorization value?"
                evidence = app.retrieval.retrieve(question)
                graph_evidence = [item for item in evidence if item.via == "graph"]
                self.assertTrue(graph_evidence)
                self.assertTrue(any(item.version_id == b_submission.version.version_id for item in graph_evidence))
                result = app.answers.query(question)
                self.assertEqual(result.outcome, Outcome.ANSWER)
                self.assertEqual(result.answer, "CERULEAN")
                self.assertEqual(len(result.citations), 1)
                self.assertEqual(result.citations[0].via, "graph")
                self.assertTrue(result.verification["citations_valid"])

    def test_failed_replacement_stays_hidden_then_retry_publishes_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            b = root / "B.txt"
            b.write_text("The approved launch code is CERULEAN.", encoding="utf-8")
            with make_app(root) as app:
                v1 = import_and_run(app, b).version
                b.write_text("The approved launch code is AMBER.", encoding="utf-8")

                def fail_after_graph(point: str):
                    if point == "after_graph":
                        raise PublicationError("injected failure")

                app.ingestion.failure_injector = fail_after_graph
                v2_submission = app.ingestion.prepare_txt_import(b)
                with self.assertRaises(PublicationError):
                    app.jobs.run_once()
                document = app.metadata.document(v1.document_id)
                self.assertEqual(document.active_version_id, v1.version_id)
                self.assertEqual(
                    app.metadata.version(v2_submission.version.version_id).status,
                    VersionStatus.FAILED,
                )

                app.ingestion.failure_injector = None
                app.metadata.retry_job(v2_submission.job.job_id)
                app.jobs.run_once()
                document = app.metadata.document(v1.document_id)
                self.assertEqual(document.active_version_id, v2_submission.version.version_id)
                expected = v2_submission.version.expected_chunk_count
                self.assertEqual(app.vector.count_version(app.installation_id, v2_submission.version.version_id), expected)
                self.assertEqual(app.graph.count_version(app.installation_id, v2_submission.version.version_id), expected)
                active = app.metadata.active_chunks(
                    [
                        chunk.chunk_id
                        for chunk in app.metadata.all_chunks_for_version(v1.version_id)
                        + app.metadata.all_chunks_for_version(v2_submission.version.version_id)
                    ]
                )
                self.assertTrue(active)
                self.assertTrue(
                    all(chunk.version_id == v2_submission.version.version_id for chunk, _ in active.values())
                )

    def test_failure_after_publication_retries_idempotently(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "published.txt"
            source.write_text("Published fact.", encoding="utf-8")

            def fail_after_publish(point: str):
                if point == "after_publish":
                    raise PublicationError("injected post-publication failure")

            with make_app(root, failure_injector=fail_after_publish) as app:
                submission = app.ingestion.prepare_txt_import(source)
                with self.assertRaises(PublicationError):
                    app.jobs.run_once()
                self.assertEqual(
                    app.metadata.document(submission.document.document_id).active_version_id,
                    submission.version.version_id,
                )
                self.assertEqual(
                    app.metadata.version(submission.version.version_id).status,
                    VersionStatus.READY,
                )
                app.ingestion.failure_injector = None
                status_changes = []
                set_status = app.metadata.set_version_status

                def track_status(version_id, status, failure_code=None):
                    status_changes.append(status)
                    return set_status(version_id, status, failure_code)

                app.metadata.set_version_status = track_status  # type: ignore[method-assign]
                app.metadata.retry_job(submission.job.job_id)
                app.jobs.run_once()
                self.assertNotIn(VersionStatus.INDEXING, status_changes)
                expected = submission.version.expected_chunk_count
                self.assertEqual(app.vector.count_version(app.installation_id, submission.version.version_id), expected)
                self.assertEqual(app.graph.count_version(app.installation_id, submission.version.version_id), expected)

    def test_unsupported_query_abstains_and_bad_citation_is_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "known.txt"
            source.write_text("Orchards contain apples.", encoding="utf-8")
            with make_app(root) as app:
                import_and_run(app, source)
                abstention = app.answers.query("quasar xenolith zeugma")
                self.assertEqual(abstention.outcome, Outcome.INSUFFICIENT_EVIDENCE)
                bad = AnswerService(app.metadata, app.retrieval, BadCitationProvider()).query("orchards apples")
                self.assertEqual(bad.outcome, Outcome.ERROR)
                self.assertEqual(bad.verification["error_code"], "invalid_provider_response")

    def test_model_payload_contract_rejects_invalid_citations_and_outcome(self):
        invalid_payloads = (
            '{"outcome":"answer","answer":"x","citation_ids":"chunk"}',
            '{"outcome":"insufficient_evidence","answer":"unknown","citation_ids":["chunk"]}',
            '{"outcome":"error","answer":"failed","citation_ids":[]}',
            '{"outcome":"CERULEAN","answer":"x","citation_ids":["chunk"]}',
            '[]',
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(ProviderResponseError):
                OllamaProvider._parse_payload(payload)

    def test_dependency_and_manifest_outages_are_errors(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = make_settings(root)
            embedding = HashEmbedding(settings.embedding_dimension)
            with GraphMindApplication(
                settings,
                vector=FailingVectorStore(embedding),
                graph=MemoryGraphStore(),
                provider=ExtractiveProvider(),
            ) as app:
                result = app.answers.query("anything")
                self.assertEqual(result.outcome, Outcome.ERROR)
                self.assertEqual(result.verification["error_code"], "dependency_unavailable")

            with make_app(root / "second") as app:
                def unavailable(_):
                    raise ManifestUnavailableError("manifest unavailable")

                app.metadata.active_chunks = unavailable  # type: ignore[method-assign]
                result = app.answers.query("anything")
                self.assertEqual(result.outcome, Outcome.ERROR)
                self.assertEqual(result.verification["error_code"], "manifest_unavailable")


if __name__ == "__main__":
    unittest.main()
