from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from pathlib import Path

from graphmind.config import Settings
from graphmind.domain import Outcome
from graphmind.providers import ExtractiveProvider, OllamaProvider
from graphmind.service import GraphMindApplication


RUN_INTEGRATION = os.environ.get("GRAPHMIND_RUN_INTEGRATION") == "1"
RUN_MODEL = os.environ.get("GRAPHMIND_RUN_MODEL_INTEGRATION") == "1"


@unittest.skipUnless(RUN_INTEGRATION, "set GRAPHMIND_RUN_INTEGRATION=1 for real stores")
class RealStoreIntegrationTests(unittest.TestCase):
    def test_txt_dual_store_and_graph_expansion(self):
        repository = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory(prefix="graphmind-real-") as temporary:
            root = Path(temporary)
            settings = Settings.load(
                dotenv_path=repository / ".env",
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
            a = root / "A.txt"
            b = root / "B.txt"
            a.write_text(
                "Zephyr initiative authorization records are maintained in [[B.txt]].",
                encoding="utf-8",
            )
            b.write_text("The approved launch code is CERULEAN.", encoding="utf-8")
            app = GraphMindApplication(settings, provider=ExtractiveProvider())
            try:
                statuses = app.readiness()
                self.assertTrue(all(status.available for status in statuses), statuses)
                for path in (a, b):
                    submission = app.ingestion.prepare_txt_import(path)
                    app.jobs.run_once()
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
                self.assertTrue(any(item.via == "graph" and item.display_name == "B.txt" for item in evidence))
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
        settings = Settings.load(dotenv_path=repository / ".env", environ={})
        settings.validate(require_provider_secret=True)
        provider = OllamaProvider(
            settings.ollama_base_url,
            settings.ollama_api_key,
            settings.answer_model,
        )
        evidence = Evidence(
            citation_id="fixture-citation",
            document_id="fixture-document",
            version_id="fixture-version",
            display_name="fixture.txt",
            locator="line 1",
            excerpt="The launch code is CERULEAN.",
            via="vector",
            score=1.0,
        )
        result = provider.answer("What is the launch code?", [evidence])
        self.assertEqual(result.outcome, Outcome.ANSWER)
        self.assertIn("fixture-citation", result.citation_ids)
        self.assertIn("CERULEAN", result.answer.upper())


if __name__ == "__main__":
    unittest.main()
