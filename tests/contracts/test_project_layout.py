"""Protect the refactor's ownership, compatibility and packaging boundaries."""
import ast
from dataclasses import FrozenInstanceError
import importlib
from pathlib import Path
import unittest

from graphmind.errors import ConfigurationError, ProviderResponseError
from graphmind.rag.components.citations import citations_for_answer
from graphmind.rag.components.contracts import Evidence, Outcome, ProviderAnswer
from graphmind.rag.registry import ENGINE_CATALOG, engine_spec
from graphmind.web.assets import ASSET_NAMES, read_asset
from tests.support.paths import REPOSITORY


class ProjectLayoutTests(unittest.TestCase):
    def test_compatibility_facades_alias_the_canonical_classes(self):
        owners = {
            "domain": {"rag.components.contracts": (
                "AdapterStatus", "Chunk", "Document", "Evidence", "Job", "JobStatus",
                "Outcome", "ProviderAnswer", "QueryResult", "SearchHit", "Version", "VersionStatus",
            )},
            "embeddings": {"rag.components.embeddings": (
                "EmbeddingProvider", "HashEmbedding", "OllamaEmbedding", "cosine_similarity",
            )},
            "retrieval": {
                "rag.components.pipeline": ("AnswerService", "LegacyReasonAdapter"),
                "rag.graphmind.engine": ("RetrievalService",),
            },
            "storage": {
                "rag.components.vector_store": ("ChromaVectorStore", "MemoryVectorStore", "VectorStore"),
                "rag.graphmind.reference_graph": ("GraphStore", "MemoryGraphStore", "Neo4jGraphStore"),
            },
        }
        for facade, modules in owners.items():
            legacy = importlib.import_module("graphmind." + facade)
            for module, names in modules.items():
                canonical = importlib.import_module("graphmind." + module)
                for name in names:
                    with self.subTest(facade=facade, name=name):
                        self.assertIs(getattr(legacy, name), getattr(canonical, name))

    def test_engine_and_answer_pipeline_have_distinct_canonical_owners(self):
        from graphmind.rag.components.pipeline import AnswerService
        from graphmind.rag.graphmind.engine import RetrievalService

        self.assertEqual(AnswerService.__module__, "graphmind.rag.components.pipeline")
        self.assertEqual(RetrievalService.__module__, "graphmind.rag.graphmind.engine")

    def test_catalogue_does_not_claim_planned_engines_are_implemented(self):
        self.assertEqual([spec.engine_id for spec in ENGINE_CATALOG],
                         ["graphmind", "tree_navigation", "multimodal"])
        self.assertTrue(engine_spec("graphmind").implemented)
        self.assertTrue(engine_spec("tree_navigation").implemented)
        self.assertFalse(engine_spec("multimodal").implemented)
        with self.assertRaises(FrozenInstanceError):
            engine_spec("graphmind").implemented = False
        with self.assertRaises(ConfigurationError):
            engine_spec("unknown")

    def test_browser_assets_match_the_single_root_source(self):
        from graphmind import product_server

        self.assertEqual(product_server.CSS, read_asset("graphmind.css"))
        self.assertEqual(product_server.JAVASCRIPT, read_asset("graphmind.js"))
        for name in ASSET_NAMES:
            self.assertEqual(read_asset(name),
                             (REPOSITORY / "assets/web" / name).read_text(encoding="utf-8"))

    def test_asset_loader_rejects_paths_and_remote_urls(self):
        for name in ("../.env", "graphmind.js/../other", "https://example.test/code.js", "unknown.js"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                read_asset(name)

    def test_citations_preserve_original_evidence_identity_and_order(self):
        first = Evidence("first", "doc", "version", "Guide", "line 1", "Fact one", "vector", 1.0)
        second = Evidence("second", "doc", "version", "Guide", "line 2", "Fact two", "graph", 0.0)
        result = citations_for_answer(ProviderAnswer(Outcome.ANSWER, "Answer", ("second", "first")),
                                      [first, second])
        self.assertIs(result[0], second)
        self.assertIs(result[1], first)

    def test_unsupported_citations_still_fail_and_abstention_stays_uncited(self):
        for citation_ids in ((), ("not-retrieved",)):
            with self.subTest(citation_ids=citation_ids), self.assertRaises(ProviderResponseError):
                citations_for_answer(ProviderAnswer(Outcome.ANSWER, "Unsupported", citation_ids), [])
        self.assertEqual(citations_for_answer(
            ProviderAnswer(Outcome.INSUFFICIENT_EVIDENCE, "Not known"), []), ())

    def test_source_assets_and_test_fixtures_remain_outside_src(self):
        self.assertTrue((REPOSITORY / "assets/web/graphmind.js").is_file())
        self.assertTrue((REPOSITORY / "tests/fixtures/evaluation/metakgp_50.json").is_file())
        self.assertFalse((REPOSITORY / "src/graphmind/tests").exists())
        self.assertFalse((REPOSITORY / "src/graphmind/assets").exists())

    def test_repository_groups_separate_product_configuration_docs_and_legacy(self):
        for relative in (
            "README.md", "documentation/ARCHITECTURE.md", "documentation/AUTH_AND_MCP.md",
            "documentation/DEPENDENCIES.md", "config/.env.example", "config/graphmind.example.toml",
            "config/constraints/core-py313.txt", "legacy/Crawler/cleaned_wiki.jsonl",
            "legacy/entrypoints/app.py", "legacy/entrypoints/mcp_server.py",
            "tools/evaluation/p4-evaluate.ps1", "deploy/legacy/deploy.sh",
        ):
            with self.subTest(relative=relative):
                self.assertTrue((REPOSITORY / relative).is_file())
        self.assertTrue((REPOSITORY / ".mcp.json").is_file())
        for relative in ("Crawler", "RAG", "neo4j", "VectorStore", "VectorStore_test"):
            with self.subTest(relative=relative):
                self.assertFalse((REPOSITORY / relative).exists())

    def test_active_package_does_not_import_archived_legacy_code(self):
        import graphmind

        for path in Path(graphmind.__file__).parent.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = ([node.module or ""] if isinstance(node, ast.ImportFrom)
                         else [alias.name for alias in node.names] if isinstance(node, ast.Import) else [])
                with self.subTest(path=path.name):
                    self.assertFalse(any(name.split(".")[0] in {"legacy", "Crawler", "RAG"}
                                         for name in names))
