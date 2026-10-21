"""Tree-only ingestion and reads work without any vector or graph service."""
from pathlib import Path
import re
import tempfile
import threading
import unittest
from unittest.mock import patch

from graphmind.errors import DependencyUnavailableError, DocumentNotFoundError, UnsupportedFileError
from graphmind.rag.components.contracts import QueryContext
from graphmind.providers import ExtractiveProvider
from graphmind.reader import ReaderService
from graphmind.service import GraphMindApplication
from graphmind.embeddings import HashEmbedding
from graphmind.storage import MemoryGraphStore, MemoryVectorStore
from tests.unit.test_ingestion_lifecycle import make_settings, pdf_bytes


class ForbiddenEmbedding:
    @property
    def fingerprint(self):
        raise AssertionError("Tree ingestion must not resolve an embedding model")


class ForbiddenVector:
    embedding = ForbiddenEmbedding()

    def close(self):
        pass

    def __getattr__(self, name):
        raise AssertionError("Tree-only operation used a vector store: " + name)


class ForbiddenGraph:
    def close(self):
        pass

    def __getattr__(self, name):
        raise AssertionError("Tree-only operation used a graph store: " + name)


class FixtureNavigator(ExtractiveProvider):
    def select_nodes(self, question, candidates, *, limit=2):
        terms = set(re.findall(r"\w+", question.casefold())) - {"the", "is", "what", "a"}
        ranked = sorted(candidates, key=lambda item: -len(terms & set(re.findall(
            r"\w+", (item["title"] + " " + item["preview"]).casefold()))))
        return [ranked[0]["node_id"]] if ranked else []


def tree_app(root):
    return GraphMindApplication(make_settings(root, enabled_engines=("tree_navigation",)),
                                vector=ForbiddenVector(), graph=ForbiddenGraph(), provider=FixtureNavigator())


class TreeNavigationTests(unittest.TestCase):
    def test_cancellation_after_navigation_stops_further_model_calls(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "manual.pdf"
            source.write_bytes(pdf_bytes(["Overview.", "Zephyr maintenance takes 47 days."]))
            outline = [{"title": "Manual", "node_id": "0000", "start_index": 1, "end_index": 2}]
            with tree_app(root) as app, patch.object(app.tree.adapter, "extract", return_value=outline):
                submission = app.ingestion.prepare_import(source)
                app.jobs.run_job(submission.job.job_id)
                cancel = threading.Event()
                def choose_then_cancel(question, candidates, **options):
                    cancel.set()
                    return [candidates[0]["node_id"]]
                with patch.object(app.answers.provider, "select_nodes", side_effect=choose_then_cancel) as navigation:
                    with patch.object(app.answers.provider, "answer") as generation:
                        result = app.answers.query("Zephyr maintenance", cancel_event=cancel,
                            context=QueryContext("tree_navigation", (submission.document.document_id,)))
                        self.assertEqual(result.verification["error_code"], "query_cancelled")
                        self.assertEqual(navigation.call_count, 1)
                        generation.assert_not_called()



    def test_real_pageindex_tree_only_ingest_answer_restart_and_idempotency(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "manual.pdf"
            original = pdf_bytes(["Deployment overview.", "The Zephyr maintenance interval is 47 days."])
            source.write_bytes(original)
            with tree_app(root) as app:
                submission = app.ingestion.prepare_import(source)
                app.jobs.run_job(submission.job.job_id)
                index = app.indexes.ready(submission.document.document_id, "tree_navigation")
                self.assertIsNotNone(index)
                self.assertEqual(index.source_version_id, submission.version.version_id)
                result = ReaderService(app).ask("What is the Zephyr maintenance interval?",
                    engine_id="tree_navigation", document_ids=(submission.document.document_id,))
                self.assertEqual(result["outcome"], "answer")
                self.assertIn("47 days", result["answer"])
                self.assertEqual(result["citations"][0]["locator"], "page 2")
                self.assertEqual((app.settings.data_dir / submission.version.private_file_ref).read_bytes(), original)
                self.assertTrue(all(item.available for item in app.readiness()))
            with tree_app(root) as restarted:
                with patch.object(restarted.tree.adapter, "extract", side_effect=AssertionError("No rebuild")):
                    repeated = restarted.ingestion.prepare_import(source)
                    self.assertEqual(repeated.job.job_id, submission.job.job_id)
                    self.assertEqual(restarted.indexes.ready(submission.document.document_id, "tree_navigation").index_id,
                                     index.index_id)
                    self.assertIn("47 days", ReaderService(restarted).ask("Zephyr maintenance interval",
                        engine_id="tree_navigation", document_ids=(submission.document.document_id,))["answer"])

    def test_failed_pdf_replacement_keeps_the_previous_tree_and_delete_hides_then_cleans_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "manual.pdf"
            source.write_bytes(pdf_bytes(["Overview.", "Zephyr maintenance takes 47 days."]))
            outline = [{"title": "Manual", "node_id": "0000", "start_index": 1, "end_index": 2}]
            with tree_app(root) as app, patch.object(app.tree.adapter, "extract", return_value=outline):
                first = app.ingestion.prepare_import(source)
                app.jobs.run_job(first.job.job_id)
                head = app.indexes.ready(first.document.document_id, "tree_navigation")
                source.write_bytes(pdf_bytes(["Overview.", "Zephyr maintenance takes 53 days."]))
                replacement = app.ingestion.prepare_import(source)
                with patch.object(app.tree.adapter, "extract", side_effect=DependencyUnavailableError("injected")):
                    with self.assertRaises(DependencyUnavailableError):
                        app.jobs.run_job(replacement.job.job_id)
                self.assertEqual(app.indexes.ready(first.document.document_id, "tree_navigation").index_id, head.index_id)
                app.jobs.run_job(replacement.job.job_id)
                self.assertIn("53 days", ReaderService(app).ask("Zephyr maintenance",
                    engine_id="tree_navigation", document_ids=(first.document.document_id,))["answer"])
                deletion = app.ingestion.prepare_delete(first.document.document_id)
                with self.assertRaises(DocumentNotFoundError):
                    ReaderService(app).search_documents("Zephyr maintenance", engine_id="tree_navigation",
                                                         document_ids=(first.document.document_id,))
                app.jobs.run_job(deletion.job.job_id)
                self.assertFalse((app.artifacts.root / head.index_id).exists())

    def test_unsupported_input_is_rejected_before_staging_or_queuing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "manual.txt"
            source.write_text("A text document.", encoding="utf-8")
            with tree_app(root) as app:
                with self.assertRaises(UnsupportedFileError):
                    app.ingestion.prepare_import(source)
                self.assertEqual(app.metadata.list_documents(), [])
                self.assertEqual(app.metadata.list_jobs(), [])

    def test_adding_graphmind_reuses_a_tree_only_source_and_publishes_selected_heads_together(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "manual.pdf"
            source.write_bytes(pdf_bytes(["Overview.", "Zephyr maintenance takes 47 days."]))
            outline = [{"title": "Manual", "node_id": "0000", "start_index": 1, "end_index": 2}]
            with tree_app(root) as first, patch.object(first.tree.adapter, "extract", return_value=outline):
                original = first.ingestion.prepare_import(source)
                first.jobs.run_job(original.job.job_id)
                old_tree = first.indexes.ready(original.document.document_id, "tree_navigation")
            settings = make_settings(root, enabled_engines=("graphmind", "tree_navigation"),
                                     retrieval_profile="hybrid", reranker_dir=str(root / "fixture-model"))
            embedding = HashEmbedding(64)
            class Reranker:
                def score(self, question, passages):
                    return [0.9] * len(passages)
            with GraphMindApplication(settings, vector=MemoryVectorStore(embedding),
                    hybrid_vector=MemoryVectorStore(embedding), graph=MemoryGraphStore(),
                    provider=FixtureNavigator(), reranker=Reranker()) as app:
                combined = app.ingestion.prepare_import(source)
                self.assertEqual(combined.version.version_id, original.version.version_id)
                self.assertEqual(combined.version.private_file_ref, original.version.private_file_ref)
                app.jobs.run_job(combined.job.job_id)
                self.assertEqual(app.indexes.ready(original.document.document_id, "tree_navigation").index_id,
                                 old_tree.index_id)
                self.assertEqual(app.indexes.ready(original.document.document_id, "graphmind").source_version_id,
                                 original.version.version_id)
                self.assertEqual(len(list(app.settings.files_dir.rglob("original.pdf"))), 1)
                source.write_bytes(pdf_bytes(["Overview.", "Zephyr maintenance takes 53 days."]))
                with patch.object(app.tree.adapter, "extract", side_effect=DependencyUnavailableError("injected")):
                    replacement = app.ingestion.prepare_import(source)
                    with self.assertRaises(DependencyUnavailableError):
                        app.jobs.run_job(replacement.job.job_id)
                self.assertEqual(app.metadata.document(original.document.document_id).active_version_id,
                                 original.version.version_id)
                self.assertEqual({app.indexes.ready(original.document.document_id, engine).source_version_id
                                  for engine in ("graphmind", "tree_navigation")}, {original.version.version_id})
