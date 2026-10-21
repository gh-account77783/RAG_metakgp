"""One shared source boundary for all engine and reader consumers."""
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest

from graphmind.errors import (DependencyUnavailableError, DocumentNotFoundError,
                              JobLeaseError, RetryableQueryError, SourceIntegrityError)
from graphmind.rag.components.contracts import Evidence, Outcome, ProviderAnswer
from graphmind.rag.components.pipeline import AnswerService
from graphmind.sources import SourceService
from tests.unit.test_ingestion_lifecycle import make_app, run_submission


class SourceAccessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "original.txt"
        self.content = b"Private original marker."
        self.source.write_bytes(self.content)
        self.app = make_app(self.root)
        self.addCleanup(self.app.close)

    def test_original_snapshot_is_bound_to_active_version_and_hides_body_in_repr(self):
        imported = run_submission(self.app, self.source)
        snapshot = self.app.sources.snapshot(imported.document.document_id)
        self.assertEqual(snapshot.version.version_id, imported.version.version_id)
        self.assertEqual(snapshot.content, self.content)
        self.assertNotIn(self.content.decode(), repr(snapshot))
        self.app.ingestion.prepare_delete(imported.document.document_id)
        with self.assertRaises(DocumentNotFoundError):
            self.app.sources.snapshot(imported.document.document_id)
        self.assertTrue((self.app.settings.data_dir / imported.version.private_file_ref).is_file())

    def test_original_integrity_and_size_limits_are_checked_before_use(self):
        imported = run_submission(self.app, self.source)
        with self.assertRaises(SourceIntegrityError):
            SourceService(self.app.metadata, self.app.files, max_file_bytes=1).snapshot(imported.document.document_id)
        stored = self.app.settings.data_dir / imported.version.private_file_ref
        stored.write_bytes(b"x" * len(self.content))
        with self.assertRaises(SourceIntegrityError):
            self.app.sources.snapshot(imported.document.document_id)
        stored.write_bytes(self.content + b"over limit")
        with self.assertRaises(DependencyUnavailableError):
            self.app.sources.snapshot(imported.document.document_id)

    def test_legacy_unverifiable_hash_requires_explicit_reimport(self):
        imported = run_submission(self.app, self.source)
        with self.app.metadata.transaction() as connection:
            connection.execute("UPDATE versions SET content_hash = ? WHERE version_id = ?",
                               ("legacy-text-hash", imported.version.version_id))
        with self.assertRaisesRegex(SourceIntegrityError, "reimport"):
            self.app.sources.snapshot(imported.document.document_id)
        self.assertEqual(self.app.metadata.version(imported.version.version_id).content_hash, "legacy-text-hash")

    def test_legacy_unknown_size_is_bounded_and_hash_verified_without_rewriting_history(self):
        imported = run_submission(self.app, self.source)
        with self.app.metadata.transaction() as connection:
            connection.execute("UPDATE versions SET source_size = 0 WHERE version_id = ?",
                               (imported.version.version_id,))
        snapshot = self.app.sources.snapshot(imported.document.document_id)
        self.assertEqual(snapshot.content, self.content)
        self.assertEqual(self.app.metadata.version(imported.version.version_id).source_size, 0)

    def test_staged_snapshot_requires_existing_job_and_writer_fences(self):
        imported = self.app.ingestion.prepare_import(self.source)
        with self.assertRaises(JobLeaseError):
            self.app.sources.snapshot_for_job(imported.job)
        self.assertTrue(self.app.metadata.acquire_writer("source-worker", 30))
        job = self.app.metadata.claim_next_job("source-worker", 30, job_id=imported.job.job_id)
        snapshot = self.app.sources.snapshot_for_job(job)
        self.assertEqual(snapshot.content, self.content)
        self.assertIsNone(self.app.metadata.document(imported.document.document_id).active_version_id)
        self.app.metadata.release_writer("source-worker")
        with self.assertRaises(JobLeaseError):
            self.app.sources.snapshot_for_job(job)

    def test_original_reader_rejects_escape_and_bounds_the_read(self):
        imported = run_submission(self.app, self.source)
        for reference in (str(self.source), "../original.txt", "private/../original.txt"):
            with self.subTest(reference=reference), self.assertRaises(DependencyUnavailableError):
                self.app.files.read_bytes(reference, max_bytes=100)
        with self.assertRaises(DependencyUnavailableError):
            self.app.files.read_bytes(imported.version.private_file_ref, max_bytes=len(self.content) - 1)


class SourceEligibilityTests(unittest.TestCase):
    @staticmethod
    def evidence(version, citation):
        return Evidence(citation, "doc-" + version, version, "source", "page 1", "original evidence", "test", 1.0)

    def test_stale_retriever_output_never_reaches_generation_or_search_delivery(self):
        calls = []
        metadata = SimpleNamespace(active_version_ids=lambda: {"current"})
        retrieval = SimpleNamespace(retrieve=lambda question: [self.evidence("stale", "c1")])
        provider = SimpleNamespace(answer=lambda *args: calls.append(args))
        service = AnswerService(metadata, retrieval, provider)
        with self.assertRaises(RetryableQueryError):
            service.query("question")
        with self.assertRaises(RetryableQueryError):
            service.search("question")
        self.assertEqual(calls, [])

    def test_withdrawn_uncited_context_invalidates_generated_answer(self):
        active = {"v1", "v2"}
        evidence = [self.evidence("v1", "c1"), self.evidence("v2", "c2")]
        calls = []

        def answer(question, context):
            calls.append(question)
            active.remove("v2")
            return ProviderAnswer(Outcome.ANSWER, "Answer using context.", ("c1",))

        service = AnswerService(SimpleNamespace(active_version_ids=lambda: set(active)),
                                SimpleNamespace(retrieve=lambda question: evidence),
                                SimpleNamespace(answer=answer))
        with self.assertRaises(RetryableQueryError):
            service.query("question")
        self.assertEqual(len(calls), 1)
