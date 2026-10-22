"""Visual lifecycle shares source/jobs/authenticated artifacts, with no vector fallback."""
import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from graphmind.domain import Evidence, Outcome
from graphmind.rag.components.contracts import QueryContext
from graphmind.embeddings import HashEmbedding
from graphmind.errors import DocumentNotFoundError, ProviderResponseError, PublicationError
from graphmind.providers import ExtractiveProvider, OllamaProvider
from graphmind.service import GraphMindApplication
from graphmind.storage import MemoryGraphStore, MemoryVectorStore
from tests.support.pdf_engine_documents import documents
from tests.unit.test_ingestion_lifecycle import make_settings


class FixtureVisualAdapter:
    def identity(self):
        return {"model": "owned-test-fixture", "revision": "1"}

    def build(self, images):
        self.images = images
        return b"owned-fixture-vectors"

    def rank(self, question, vectors, *, page_count):
        return [(2, 0.9), (1, 0.8), (3, 0.7)]


class VisualLifecycleTests(unittest.TestCase):
    def test_visual_build_default_and_host_setting_are_bounded(self):
        from graphmind.config import Settings
        from graphmind.errors import ConfigurationError
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(make_settings(root).visual_build_timeout_seconds, 900)
            settings = Settings.load(dotenv_path=root / "missing.env", environ={
                "GRAPHMIND_VISUAL_BUILD_TIMEOUT_SECONDS": "600"}, explicit={
                    "data_dir": root / "data", "allowed_import_roots": (root,)})
            self.assertEqual(settings.visual_build_timeout_seconds, 600)
            with self.assertRaises(ConfigurationError):
                make_settings(root, visual_build_timeout_seconds=1801)

    def application(self, root, adapter, *, engines=("multimodal",)):
        return GraphMindApplication(make_settings(root, enabled_engines=engines),
            vector=MemoryVectorStore(HashEmbedding(64)), graph=MemoryGraphStore(),
            provider=ExtractiveProvider(), visual_adapter=adapter)

    def test_unavailable_visual_model_keeps_ready_tree_engine_available(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            adapter = FixtureVisualAdapter()
            with self.application(root, adapter, engines=("tree_navigation", "multimodal")) as app:
                with patch.object(adapter, "identity", side_effect=RuntimeError("fixture model unavailable")):
                    self.assertTrue(all(status.available for status in app.reader_readiness()))
                    states = app.engine_readiness()
                    self.assertTrue(all(status.available for status in states["tree_navigation"]))
                    self.assertFalse(all(status.available for status in states["multimodal"]))

    def test_original_rasters_citations_restart_replace_failure_and_delete(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "visual.pdf"
            source.write_bytes(documents()["visual.pdf"])
            adapter = FixtureVisualAdapter()
            with self.application(root, adapter) as app:
                with patch.object(app.vector, "upsert_version", side_effect=AssertionError("no vector fallback")), \
                     patch.object(app.graph, "upsert_version", side_effect=AssertionError("no graph fallback")):
                    submitted = app.ingestion.prepare_import(source)
                    app.jobs.run_job(submitted.job.job_id)
                context = QueryContext("multimodal", (submitted.document.document_id,))
                evidence = app.router.retrieve_scoped("Which bar is tallest?", context)
                self.assertEqual(len(evidence), 1)  # One best original page per visual answer.
                self.assertEqual(evidence[0].page_number, 2)
                self.assertEqual(evidence[0].citation_id, f"page:{submitted.version.version_id}:2")
                self.assertTrue(base64.b64decode(evidence[0].image_base64).startswith(b"\x89PNG"))
                self.assertNotIn("image_base64", repr(evidence[0]))
                self.assertEqual(len(adapter.images), 6)
                head = app.indexes.ready(submitted.document.document_id, "multimodal")
                source.write_bytes(documents()["tree.pdf"])
                replacement = app.ingestion.prepare_import(source)
                with patch.object(adapter, "build", side_effect=PublicationError("injected")):
                    with self.assertRaises(PublicationError):
                        app.jobs.run_job(replacement.job.job_id)
                self.assertEqual(app.indexes.ready(submitted.document.document_id, "multimodal"), head)
            with self.application(root, adapter) as restarted:
                self.assertEqual(restarted.router.retrieve_scoped("Which bar?", context)[0].version_id,
                                 submitted.version.version_id)
                deletion = restarted.ingestion.prepare_delete(submitted.document.document_id)
                with self.assertRaises(DocumentNotFoundError):
                    restarted.router.retrieve_scoped("Which bar?", context)
                restarted.jobs.run_job(deletion.job.job_id)
                self.assertEqual(list(restarted.artifacts.root.glob("*/manifest.json")), [])

    def test_visual_facts_are_absent_from_extracted_pdf_text(self):
        import io
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(documents()["visual.pdf"]))
        extracted = "\n".join(page.extract_text() for page in reader.pages).casefold()
        for term in ("blue", "green", "triangle", "increase"):
            self.assertNotIn(term, extracted)
        fixture = json.loads((Path(__file__).parents[1] / "fixtures/evaluation/pdf_engines_12.json").read_text())
        self.assertEqual(len(fixture["cases"]), 12)
        self.assertEqual(sum(case["engine"] == "multimodal" for case in fixture["cases"]), 6)


class VisionProviderTests(unittest.TestCase):
    def test_image_input_requires_local_vision_and_stays_out_of_serialized_citations(self):
        from graphmind.domain import QueryResult
        class Transport:
            calls = []
            vision = True
            def request_json(self, method, url, **kwargs):
                self.calls.append((url, kwargs.get("payload")))
                if url.endswith("/show"):
                    return {"capabilities": ["vision"] if self.vision else []}
                return {"message": {"content": json.dumps({"outcome": "answer", "answer": "Blue.",
                                                            "citation_ids": ["E1"]})}}
        transport = Transport()
        item = Evidence("page:v:1", "doc", "v", "Owned.pdf", "page 1", "Panel One", "visual", 1,
                        page_number=1, image_base64="cG5n")
        with self.assertRaises(ProviderResponseError):
            ExtractiveProvider().answer("Panel One?", [item])
        provider = OllamaProvider("http://127.0.0.1:21434", "", "fixture", mode="local", transport=transport)
        answer = provider.answer("Which color?", [item])
        self.assertEqual(answer.citation_ids, ("page:v:1",))
        sent = json.loads(transport.calls[-1][1]["messages"][1]["content"])["evidence"]
        self.assertEqual((sent[0]["citation_id"], sent[0]["image_number"]), ("E1", "1"))
        self.assertEqual(transport.calls[-1][1]["messages"][1]["images"], ["cG5n"])
        serialized = QueryResult(Outcome.ANSWER, answer.answer, (item,), "req").as_dict()
        self.assertNotIn("cG5n", json.dumps(serialized))
        self.assertEqual(serialized["citations"][0]["page_number"], 1)
        transport.vision = False
        before = len(transport.calls)
        with self.assertRaises(ProviderResponseError):
            provider.answer("Which color?", [item])
        self.assertEqual(len(transport.calls), before + 1)  # metadata only, no image generation
        provider.mode, provider.api_key = "hosted", "fixture-placeholder"
        before = len(transport.calls)
        with self.assertRaises(ProviderResponseError):
            provider.answer("Which color?", [item])
        self.assertEqual(len(transport.calls), before)
