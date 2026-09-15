from __future__ import annotations

import io
import os
import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path

from graphmind.config import Settings
from graphmind.domain import Outcome
from graphmind.embeddings import OllamaEmbedding
from graphmind.providers import ExtractiveProvider, OllamaProvider
from graphmind.service import GraphMindApplication


RUN_INTEGRATION = os.environ.get("GRAPHMIND_RUN_INTEGRATION") == "1"
RUN_MODEL = os.environ.get("GRAPHMIND_RUN_MODEL_INTEGRATION") == "1"
RUN_LOCAL_MODEL = os.environ.get("GRAPHMIND_RUN_LOCAL_MODEL_INTEGRATION") == "1"


def _test_dotenv(repository: Path) -> Path:
    configured = os.environ.get("GRAPHMIND_TEST_DOTENV")
    return Path(configured).expanduser().resolve() if configured else repository / ".env"


def _docx_bytes() -> bytes:
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:body><w:p><w:r><w:t>DOCX marker DELTA</w:t></w:r></w:p></w:body></w:document>'
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", xml)
        archive.writestr("[Content_Types].xml", "<Types/>")
    return output.getvalue()


def _pdf_bytes() -> bytes:
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject(
                {NameObject("/F1"): writer._add_object(font)}
            )
        }
    )
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 72 720 Td (PDF marker EPSILON) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


@unittest.skipUnless(RUN_INTEGRATION, "set GRAPHMIND_RUN_INTEGRATION=1 for real stores")
class RealStoreIntegrationTests(unittest.TestCase):
    def test_all_formats_dual_store_graph_expansion_delete_and_restart(self):
        repository = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory(prefix="graphmind-real-") as temporary:
            root = Path(temporary)
            settings = Settings.load(
                dotenv_path=_test_dotenv(repository),
                environ={},
                explicit={
                    "data_dir": root / "runtime",
                    "allowed_import_roots": (root,),
                    "collection_name": f"gmtest_{uuid.uuid4().hex}",
                    "chunk_size": 200,
                    "chunk_overlap": 20,
                    "embedding_dimension": 64,
                },
            )
            a = root / "A.md"
            b = root / "B.csv"
            a.write_text(
                "# Zephyr\nAuthorization records are maintained in [[B.csv]].",
                encoding="utf-8",
            )
            b.write_text("subject,value\nlaunch code,CERULEAN\n", encoding="utf-8")
            txt = root / "notes.txt"
            txt.write_text("TXT marker GAMMA.", encoding="utf-8")
            docx = root / "report.docx"
            docx.write_bytes(_docx_bytes())
            pdf = root / "manual.pdf"
            pdf.write_bytes(_pdf_bytes())
            app = GraphMindApplication(settings, provider=ExtractiveProvider())
            try:
                statuses = app.readiness()
                self.assertTrue(all(status.available for status in statuses), statuses)
                submissions = []
                for path in (a, b, txt, docx, pdf):
                    submission = app.ingestion.prepare_import(path)
                    app.jobs.run_once()
                    submissions.append(submission)
                    expected = submission.version.expected_chunk_count
                    self.assertEqual(
                        app.vector.count_version(app.installation_id, submission.version.version_id),
                        expected,
                    )
                    self.assertEqual(
                        app.graph.count_version(app.installation_id, submission.version.version_id),
                        expected,
                    )
                app.retrieval.seed_limit = 1
                evidence = app.retrieval.retrieve("Where are Zephyr initiative authorization records?")
                self.assertTrue(any(item.via == "graph" and item.display_name == "B.csv" for item in evidence))

                deleted = submissions[-1]
                deletion = app.ingestion.prepare_delete(deleted.document.document_id)
                self.assertNotIn(deleted.version.version_id, app.metadata.active_version_ids())
                app.close()
                app = GraphMindApplication(settings, provider=ExtractiveProvider())
                app.jobs.reconcile()
                app.jobs.run_once()
                self.assertEqual(app.metadata.job(deletion.job.job_id).status.value, "succeeded")
                self.assertEqual(
                    app.vector.count_version(app.installation_id, deleted.version.version_id), 0
                )
                self.assertEqual(
                    app.graph.count_version(app.installation_id, deleted.version.version_id), 0
                )
                self.assertFalse((settings.files_dir / deleted.document.document_id).exists())
            finally:
                if hasattr(app.vector, "delete_installation"):
                    app.vector.delete_installation(app.installation_id)
                if hasattr(app.graph, "delete_installation"):
                    app.graph.delete_installation(app.installation_id)
                app.close()


@unittest.skipUnless(RUN_MODEL, "set GRAPHMIND_RUN_MODEL_INTEGRATION=1 for a hosted model call")
class RealModelIntegrationTests(unittest.TestCase):
    def test_configured_model_returns_valid_grounded_json(self):
        from graphmind.domain import Evidence

        repository = Path(__file__).resolve().parents[2]
        settings = Settings.load(dotenv_path=_test_dotenv(repository), environ={})
        settings.validate(require_provider_secret=True)
        provider = OllamaProvider(
            settings.ollama_base_url,
            settings.ollama_api_key,
            settings.answer_model,
        )
        evidence = [
            Evidence(
                citation_id="fixture-a",
                document_id="fixture-document-a",
                version_id="fixture-version-a",
                display_name="A.txt",
                locator="line 1",
                excerpt=(
                    "The blue lantern protocol controls Zephyr authorization values. "
                    "The answer record is maintained in B.txt."
                ),
                via="vector",
                score=1.0,
            ),
            Evidence(
                citation_id="fixture-b",
                document_id="fixture-document-b",
                version_id="fixture-version-b",
                display_name="B.txt",
                locator="line 1",
                excerpt="The requested authorization value is CERULEAN.",
                via="graph",
                score=0.0,
            ),
        ]
        result = provider.answer(
            "Under the blue lantern protocol for Zephyr, what is the authorization value?",
            evidence,
        )
        self.assertEqual(result.outcome, Outcome.ANSWER)
        self.assertIn("fixture-b", result.citation_ids)
        self.assertIn("CERULEAN", result.answer.upper())


@unittest.skipUnless(
    RUN_LOCAL_MODEL,
    "set GRAPHMIND_RUN_LOCAL_MODEL_INTEGRATION=1 for local Ollama models",
)
class RealLocalModelIntegrationTests(unittest.TestCase):
    def test_local_bge_m3_and_gemma_return_identified_grounded_results(self):
        from graphmind.domain import Evidence

        base_url = os.environ.get(
            "GRAPHMIND_LOCAL_OLLAMA_BASE_URL", "http://127.0.0.1:11434"
        )
        embedding_model = os.environ.get(
            "GRAPHMIND_LOCAL_EMBEDDING_MODEL", "bge-m3"
        )
        answer_model = os.environ.get("GRAPHMIND_LOCAL_ANSWER_MODEL", "gemma4:e2b")
        embedding = OllamaEmbedding(
            base_url,
            embedding_model,
            "BAAI/bge-m3",
            dimension=1024,
        )
        embedding_identity = embedding.identity()
        self.assertEqual(embedding_identity["dimension"], 1024)
        self.assertEqual(len(str(embedding_identity["revision"])), 64)
        vector = embedding.embed("The launch code is CERULEAN.")
        self.assertEqual(len(vector), 1024)

        provider = OllamaProvider(
            base_url,
            "",
            answer_model,
            mode="local",
            timeout=120,
            max_retries=0,
        )
        provider_identity = provider.identity(resolve=True)
        self.assertEqual(provider_identity["mode"], "local")
        self.assertEqual(len(str(provider_identity["digest"])), 64)
        evidence = [
            Evidence(
                citation_id="fixture-a",
                document_id="fixture-document-a",
                version_id="fixture-version-a",
                display_name="A.txt",
                locator="line 1",
                excerpt=(
                    "The blue lantern protocol controls Zephyr authorization values. "
                    "The answer record is maintained in B.txt."
                ),
                via="vector",
                score=1.0,
            ),
            Evidence(
                citation_id="fixture-b",
                document_id="fixture-document-b",
                version_id="fixture-version-b",
                display_name="B.txt",
                locator="line 1",
                excerpt="The requested authorization value is CERULEAN.",
                via="graph",
                score=0.0,
            ),
        ]
        result = provider.answer(
            "Under the blue lantern protocol for Zephyr, what is the authorization value?",
            evidence,
        )
        self.assertEqual(result.outcome, Outcome.ANSWER)
        self.assertIn("fixture-b", result.citation_ids)
        self.assertIn("CERULEAN", result.answer.upper())


if __name__ == "__main__":
    unittest.main()
