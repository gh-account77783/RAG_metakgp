"""Hybrid retrieval primitives retain active-source and job ownership checks."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from graphmind.errors import PublicationError
from graphmind.embeddings import HashEmbedding
from graphmind.providers import ExtractiveProvider
from graphmind.service import GraphMindApplication
from graphmind.storage import MemoryGraphStore, MemoryVectorStore
from graphmind.rag.components.artifacts import DerivedArtifactStore
from graphmind.rag.components.index_contracts import IndexRecipe
from graphmind.rag.components.ranking import reciprocal_rank_fusion
from graphmind.rag.graphmind.lexical import LexicalIndex
from tests.unit.test_ingestion_lifecycle import make_app, make_settings, run_submission


class FixtureReranker:
    def score(self, question, passages):
        return [0.9] * len(passages)

    def score_pairs(self, pairs):
        return [0.9] * len(pairs)


class NoDenseHits(MemoryVectorStore):
    def search(self, *args, **kwargs):
        return []


class HybridPrimitiveTests(unittest.TestCase):
    def test_origin_query_keeps_literal_page_identity_for_reference_questions(self):
        from graphmind.rag.graphmind.hybrid import HybridGraphMind
        self.assertEqual(HybridGraphMind._seed_query(
            "The Orion page links to its instructor. When did she join?"), "The Orion page links to its instructor.")
        self.assertEqual(HybridGraphMind._seed_query(
            "According to the Vega course page, when did its professor join?"),
            "According to the Vega course page")
        for question in ("What is a page?", "Compare both instructors between the Orion page and Vega page."):
            self.assertEqual(HybridGraphMind._seed_query(question), question)

    def test_origin_query_preserves_the_role_before_a_literal_source_title(self):
        from graphmind.rag.graphmind.hybrid import HybridGraphMind
        examples = {
            "In what year did the first instructor listed on the Orion Systems page join?":
                "Orion Systems page first instructor listed",
            "When did the professor linked from the Vega document join?":
                "Vega document professor linked",
            "Which society is linked under Activities in the Newcomer's guide, and name an activity on that society's page?":
                "Newcomer's guide society is linked under Activities",
        }
        for question, expected in examples.items():
            with self.subTest(question=question):
                self.assertEqual(HybridGraphMind._seed_query(question), expected)
        for question in ("When did the Orion instructor join?",
                         "Who teaches on the Orion page?",
                         "What does the society do?",
                         "Compare the first instructor on the Orion page with the professor from the Vega document."):
            with self.subTest(question=question):
                self.assertEqual(HybridGraphMind._seed_query(question), question)

    def test_fusion_uses_ranks_without_counting_duplicates_twice(self):
        self.assertEqual(reciprocal_rank_fusion(["a", "b"], ["b", "c"]), ["b", "a", "c"])
        self.assertEqual(reciprocal_rank_fusion(["a", "a"], ["b"]), ["a", "b"])

    def test_lexical_publication_and_tombstone_follow_the_existing_source_head(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "Orion Guide.txt"
            path.write_text("The rare zephyr token identifies this original passage.", encoding="utf-8")
            with make_app(root) as app:
                submission = run_submission(app, path)
                recipe = IndexRecipe.create("graphmind", "hybrid-context-v1", {})
                queued = app.indexes.submit(submission.version.version_id, recipe, max_queued_jobs=10)
                self.assertTrue(app.metadata.acquire_writer("hybrid-test", 30))
                job = app.metadata.claim_next_job("hybrid-test", 30, job_id=queued.job_id)
                index_id = recipe.index_id(app.installation_id, job.version_id)
                app.indexes.start(job, index_id)
                lexical = LexicalIndex(app.indexes)
                passages = [{"unit_id": "fixture-unit", "locator": "line 1", "text": path.read_text()}]
                lexical.replace(job, index_id, path.name, passages)
                self.assertEqual(lexical.search("zephyr", index_ids={index_id}), [])
                store = DerivedArtifactStore(app.settings.data_dir, app.indexes)
                manifest = store.publish(job, index_id, ["fixture-unit"],
                                         {"passages.json": json.dumps(passages).encode()})
                app.indexes.complete(job, index_id, manifest.inventory, artifact_ref=manifest.reference,
                                      artifact_digest=manifest.digest)
                app.indexes.activate(job)
                self.assertEqual(lexical.search("zephyr", index_ids={index_id})[0].chunk_id, "fixture-unit")
                self.assertEqual(lexical.search('title: "OR" NEAR(zephyr)', index_ids={index_id})[0].chunk_id,
                                 "fixture-unit")
                self.assertEqual(lexical.search("???", index_ids={index_id}), [])
                self.assertEqual(len(app.indexes.current("graphmind", revision="hybrid-context-v1")), 1)
                app.metadata.finish_job(job.job_id, "hybrid-test", succeeded=True, job=job)
                app.metadata.release_writer("hybrid-test")
                app.ingestion.prepare_delete(job.document_id)
                self.assertEqual(lexical.search("zephyr", index_ids={index_id}), [])
                self.assertEqual(app.indexes.current("graphmind"), [])

    def test_reference_targets_collect_links_outside_seed_chunks_and_exclude_deleted_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "course.txt"
            first.write_text("Course overview.\n" * 16 + "Instructor: [[faculty.txt]]", encoding="utf-8")
            target = root / "faculty.txt"
            target.write_text("Dr Rowan teaches this course.", encoding="utf-8")
            with make_app(root) as app:
                course = run_submission(app, first)
                faculty = run_submission(app, target)
                seed = app.metadata.all_chunks_for_version(course.version.version_id)[0]
                self.assertEqual(seed.reference_names, ())
                arguments = (app.installation_id, [seed.chunk_id])
                self.assertEqual(app.graph.reference_targets(*arguments,
                    active_version_ids=app.metadata.active_version_ids()), [faculty.document.document_id])
                app.ingestion.prepare_delete(faculty.document.document_id)
                self.assertEqual(app.graph.reference_targets(*arguments,
                    active_version_ids=app.metadata.active_version_ids()), [])


class HybridLifecycleTests(unittest.TestCase):
    def test_duplicate_seed_aliases_leave_room_for_the_reference_relationship(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            course, faculty = root / "course.md", root / "faculty.md"
            course.write_text("# OR123\n\nSource URL: https://owned.example/OR123\n\n"
                              "## Autumn 2030\n\nInstructor: [[faculty.md]]\n", encoding="utf-8")
            faculty.write_text("# Dr Rowan\n\nYear joined: 2009.\n", encoding="utf-8")
            with self.application(root) as app:
                run_submission(app, course)
                run_submission(app, faculty)
                class Reranker:
                    seed_questions = []
                    target_questions = []
                    def score(self, question, passages):
                        self.seed_questions.append(question)
                        return [0.99 if "Source URL:" in item else 0.8 if "Instructor:" in item
                                else 0.001 for item in passages]
                    def score_pairs(self, pairs):
                        self.target_questions.extend(question for question, _ in pairs)
                        return [0.9 if "Year joined:" in item else 0.001 for _, item in pairs]
                app.hybrid.reranker = Reranker()
                question = "When did the first instructor listed on the OR123 page join?"
                evidence = app.retrieval.retrieve(question)
                self.assertTrue(any("Instructor:" in item.excerpt for item in evidence))
                self.assertTrue(any("Year joined:" in item.excerpt for item in evidence))
                self.assertEqual(app.hybrid.reranker.seed_questions, ["OR123 page first instructor listed"])
                self.assertTrue(app.hybrid.reranker.target_questions)
                self.assertTrue(all(question in item for item in app.hybrid.reranker.target_questions))

    def test_reference_ranking_binds_original_target_title_and_keeps_both_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            course, faculty = root / "course.md", root / "opaque-name.md"
            course.write_text("# OR123\n\nInstructor: [[opaque-name.md]]\n", encoding="utf-8")
            faculty.write_text("# Dr Rowan\n\nYear joined: 2009.\n", encoding="utf-8")
            with self.application(root) as app:
                first, target = run_submission(app, course), run_submission(app, faculty)
                class PronounReranker:
                    pairs = []
                    def score(self, question, passages):
                        return [0.9 if "Instructor:" in passage else 0.001 for passage in passages]
                    def score_pairs(self, pairs):
                        self.pairs.extend(pairs)
                        return [0.8 if "Referenced document: Dr Rowan" in question and "2009" in passage
                                else 0.001 for question, passage in pairs]
                reranker = app.hybrid.reranker = PronounReranker()
                evidence = app.retrieval.retrieve("When did the OR123 instructor join?")
                self.assertEqual({item.document_id for item in evidence},
                                 {first.document.document_id, target.document.document_id})
                linked = [item for item in evidence if item.document_id == target.document.document_id]
                self.assertEqual(len(linked), 1)  # heading/body aliases share one original passage
                self.assertEqual(linked[0].source_title, "Dr Rowan")
                self.assertEqual(linked[0].via, "graph")
                self.assertTrue(reranker.pairs)

    def application(self, root, *, vector=None, graph=None, baseline=None, chunk_size=2200):
        settings = make_settings(root, retrieval_profile="hybrid", reranker_dir=str(root / "fixture-model"),
                                 chunk_size=chunk_size, chunk_overlap=12)
        embedding = HashEmbedding(settings.embedding_dimension)
        return GraphMindApplication(settings, vector=baseline or MemoryVectorStore(embedding),
            hybrid_vector=vector or NoDenseHits(embedding), graph=graph or MemoryGraphStore(),
            provider=ExtractiveProvider(), reranker=FixtureReranker())

    def test_lexical_only_fact_keeps_the_original_passage_beyond_800_characters(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "operations.txt"
            path.write_text("Background details. " * 80 + "The zephyr maintenance interval is 47 days.", encoding="utf-8")
            with self.application(root) as app:
                submission = run_submission(app, path)
                evidence = app.retrieval.retrieve("What is the zephyr maintenance interval?")
                self.assertTrue(evidence)
                self.assertIn("47 days", evidence[0].excerpt)
                self.assertGreater(len(evidence[0].excerpt), 800)
                self.assertEqual(evidence[0].version_id, submission.version.version_id)
                self.assertEqual(app.vector.count_version(app.installation_id, submission.version.version_id), 0)
                self.assertTrue(app.audit_manifest().available)
                deletion = app.ingestion.prepare_delete(submission.document.document_id)
                self.assertEqual(app.retrieval.retrieve("zephyr maintenance"), [])
                app.jobs.run_job(deletion.job.job_id)
                self.assertEqual(list(app.artifacts.root.glob("*/manifest.json")), [])

    def test_failed_replacement_preserves_the_old_source_and_ready_index_then_retry_publishes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "source.txt"
            path.write_text("Zephyr maintenance is 47 days.", encoding="utf-8")
            with self.application(root) as app:
                previous = run_submission(app, path)
                head = app.indexes.ready(previous.document.document_id, "graphmind")
                path.write_text("Zephyr maintenance is now 53 days.", encoding="utf-8")
                replacement = app.ingestion.prepare_import(path)
                with patch.object(app.hybrid.vector, "upsert_version", side_effect=PublicationError("injected")):
                    with self.assertRaises(PublicationError):
                        app.jobs.run_job(replacement.job.job_id)
                self.assertEqual(app.indexes.ready(previous.document.document_id, "graphmind").index_id, head.index_id)
                self.assertIn("47 days", app.retrieval.retrieve("zephyr maintenance")[0].excerpt)
                app.jobs.run_job(replacement.job.job_id)
                self.assertIn("53 days", app.retrieval.retrieve("zephyr maintenance")[0].excerpt)

    def test_switching_from_legacy_attaches_a_recipe_without_replacing_the_source_or_old_vectors(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "source.txt"
            path.write_text("Zephyr maintenance is 47 days.", encoding="utf-8")
            embedding = HashEmbedding(64)
            baseline, graph = MemoryVectorStore(embedding), MemoryGraphStore()
            with GraphMindApplication(make_settings(root), vector=baseline, graph=graph,
                                      provider=ExtractiveProvider()) as previous:
                original = run_submission(previous, path)
                old_count = baseline.count_version(previous.installation_id, original.version.version_id)
            with self.application(root, baseline=baseline, graph=graph, chunk_size=96) as app:
                submission = app.ingestion.prepare_import(path)
                self.assertEqual(submission.version.version_id, original.version.version_id)
                self.assertEqual(submission.version.private_file_ref, original.version.private_file_ref)
                app.jobs.run_job(submission.job.job_id)
                self.assertEqual(baseline.count_version(app.installation_id, original.version.version_id), old_count)
                self.assertEqual(app.indexes.ready(submission.document.document_id, "graphmind").recipe.revision,
                                 "hybrid-context-v1")
                self.assertIn("47 days", app.retrieval.retrieve("zephyr maintenance")[0].excerpt)

    def test_reference_traversal_retrieves_the_relevant_late_passage_inside_its_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            course, faculty = root / "course.txt", root / "faculty.txt"
            course.write_text("Orion course overview.\n" * 16 + "Staff: [[faculty.txt]]", encoding="utf-8")
            faculty.write_text("Unrelated faculty archive.\n" * 25 + "The instructor is Dr Rowan.", encoding="utf-8")
            with self.application(root, chunk_size=96) as app:
                source = run_submission(app, course)
                target = run_submission(app, faculty)
                target_id = app.indexes.ready(target.document.document_id, "graphmind").index_id
                search = app.hybrid.lexical.search

                def global_candidates_miss_the_target(query, *, index_ids, limit=50):
                    # Simulate the target falling outside the global candidate pool.
                    selected = index_ids - {target_id} if len(index_ids) > 1 else index_ids
                    return search(query, index_ids=selected, limit=limit)

                with patch.object(app.hybrid.lexical, "search", side_effect=global_candidates_miss_the_target):
                    evidence = app.retrieval.retrieve("Who is the Orion instructor?")
                linked = [item for item in evidence if item.document_id == target.document.document_id]
                self.assertTrue(linked)
                self.assertTrue(any("Dr Rowan" in item.excerpt and item.via == "graph" for item in linked))
                self.assertTrue(any(item.document_id == source.document.document_id for item in evidence))
