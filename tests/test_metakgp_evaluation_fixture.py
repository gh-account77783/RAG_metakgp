from __future__ import annotations

import hashlib
import json
import unittest
from collections import Counter
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
FIXTURE_PATH = REPOSITORY / "tests" / "fixtures" / "evaluation" / "metakgp_50.json"
SOURCE_PATH = REPOSITORY / "Crawler" / "cleaned_wiki.jsonl"


class MetaKGPEvaluationFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        cls.source_bytes = SOURCE_PATH.read_bytes()
        cls.documents = [
            json.loads(line)
            for line in cls.source_bytes.decode("utf-8").splitlines()
            if line.strip()
        ]
        cls.documents_by_url = {document["url"]: document for document in cls.documents}

    def test_frozen_shape_and_category_balance(self) -> None:
        cases = self.fixture["cases"]
        self.assertEqual(self.fixture["schema_version"], 1)
        self.assertEqual(self.fixture["dataset_id"], "metakgp-50-v1")
        self.assertEqual(len(cases), 50)
        self.assertEqual(
            Counter(case["category"] for case in cases),
            Counter({"direct": 30, "graph": 10, "multi_document": 5, "abstain": 5}),
        )
        self.assertEqual(
            [case["id"] for case in cases],
            [f"GMQ{number:03d}" for number in range(1, 51)],
        )
        self.assertEqual(len({case["question"] for case in cases}), 50)

    def test_source_snapshot_and_evidence_are_exact(self) -> None:
        source = self.fixture["source"]
        self.assertEqual(source["document_count"], len(self.documents))
        self.assertEqual(source["sha256"], hashlib.sha256(self.source_bytes).hexdigest())

        for case in self.fixture["cases"]:
            with self.subTest(case=case["id"]):
                if case["expected_outcome"] == "abstain":
                    self.assertEqual(case["evidence"], [])
                    self.assertTrue(case["abstention_reason"])
                    continue

                self.assertEqual(case["expected_outcome"], "answer")
                self.assertTrue(case["evidence"])
                reference_answer = case["reference_answer"].casefold()
                for term in case["required_answer_terms"]:
                    self.assertIn(term.casefold(), reference_answer)
                for evidence in case["evidence"]:
                    document = self.documents_by_url[evidence["url"]]
                    self.assertEqual(evidence["title"], document["title"])
                    for supporting_text in evidence["supporting_text"]:
                        self.assertIn(supporting_text, document["content"])

                if case["category"] == "direct":
                    self.assertEqual(len(case["evidence"]), 1)
                elif case["category"] in {"graph", "multi_document"}:
                    self.assertGreaterEqual(len(case["evidence"]), 2)

    def test_graph_cases_follow_edges_in_the_snapshot(self) -> None:
        graph_cases = [case for case in self.fixture["cases"] if case["category"] == "graph"]
        for case in graph_cases:
            with self.subTest(case=case["id"]):
                self.assertTrue(case["graph_edges"])
                for edge in case["graph_edges"]:
                    source = self.documents_by_url[edge["from_url"]]
                    target = self.documents_by_url[edge["to_url"]]
                    targets = {
                        linked["url"].split("#", 1)[0]
                        for linked in source.get("linked_to", [])
                    }
                    self.assertIn(edge["to_url"], targets)
                    self.assertNotIn(target["title"].casefold(), case["question"].casefold())


if __name__ == "__main__":
    unittest.main()
