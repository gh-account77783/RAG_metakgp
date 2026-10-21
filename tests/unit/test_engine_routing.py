"""Reader selection shares admission and source eligibility across engines."""
from pathlib import Path
import tempfile
import unittest

from graphmind.errors import EngineUnavailableError
from graphmind.reader import ReaderService
from graphmind.rag.components.contracts import QueryContext
from tests.unit.test_ingestion_lifecycle import make_app, run_submission


class EngineRoutingTests(unittest.TestCase):
    def test_reader_document_scope_and_unconfigured_engine_fail_without_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            a, b = root / "a.txt", root / "b.txt"
            a.write_text("Cerulean is the deployment color.", encoding="utf-8")
            b.write_text("Amber is another deployment color.", encoding="utf-8")
            with make_app(root) as app:
                first, second = run_submission(app, a), run_submission(app, b)
                reader = ReaderService(app)
                result = reader.search_documents("deployment color", document_ids=[first.document.document_id])
                self.assertTrue(result["results"])
                self.assertEqual({item["document_id"] for item in result["results"]}, {first.document.document_id})
                with self.assertRaises(EngineUnavailableError):
                    reader.search_documents("deployment color", engine_id="tree_navigation",
                                            document_ids=[second.document.document_id])
                app.ingestion.prepare_delete(first.document.document_id)
                from graphmind.errors import DocumentNotFoundError
                with self.assertRaises(DocumentNotFoundError):
                    reader.search_documents("deployment color", document_ids=[first.document.document_id])

    def test_context_rejects_invalid_selection_and_keeps_unique_opaque_identifiers(self):
        self.assertEqual(QueryContext("graphmind", ["doc-1", "doc-1"]).document_ids, ("doc-1",))
        for engine, documents in (({}, ()), ("unknown", ()), ("graphmind", "doc-1"),
                                  ("graphmind", ["../outside"])):
            with self.subTest(engine=engine), self.assertRaises(ValueError):
                QueryContext(engine, documents)
