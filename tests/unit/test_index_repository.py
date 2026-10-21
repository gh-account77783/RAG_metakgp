"""Engine state uses the existing source database and job-attempt authority."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from graphmind.errors import JobInputError, JobLeaseError, PublicationError
from graphmind.metadata import MetadataStore, utc_now
from graphmind.rag.components.contracts import Chunk, Job, JobStatus, VersionStatus
from graphmind.rag.components.index_contracts import IndexInventory, IndexRecipe, IndexStatus
from graphmind.rag.components.indexes import IndexRepository, legacy_graphmind_recipe
from tests.unit.test_ingestion_lifecycle import make_app, run_submission


class IndexRepositoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "source.txt"
        self.path.write_text("The original source stays available.", encoding="utf-8")
        self.app = make_app(self.root)
        self.addCleanup(self.app.close)
        self.original = run_submission(self.app, self.path)
        self.indexes = IndexRepository(self.app.metadata, self.app.sources)

    def claim(self, job):
        self.assertTrue(self.app.metadata.acquire_writer("index-test", 30))
        claimed = self.app.metadata.claim_next_job("index-test", 30, job_id=job.job_id)
        self.assertIsNotNone(claimed)
        return claimed

    def finish(self, job, *, succeeded=True):
        self.app.metadata.finish_job(job.job_id, "index-test", succeeded=succeeded, job=job)
        self.app.metadata.release_writer("index-test")

    def request(self, revision="v1"):
        recipe = IndexRecipe.create("test_engine", revision, {"model_revision": "fixture"})
        job = self.indexes.submit(self.original.version.version_id, recipe, max_queued_jobs=10)
        return self.claim(job), json.loads(job.payload_json)["index_ids"][0]

    def complete(self, job, index_id, *, activate=True):
        self.indexes.start(job, index_id)
        self.indexes.complete(job, index_id, IndexInventory.from_unit_ids(["page:1"]), activate=activate,
                              artifact_ref=f"derived/{index_id}/manifest.json",
                              artifact_digest=hashlib.sha256(b"verified fixture manifest").hexdigest())

    def test_legacy_publication_registers_a_ready_graphmind_inventory(self):
        index = self.indexes.ready(self.original.document.document_id, "graphmind")
        self.assertIsNotNone(index)
        self.assertEqual(index.status, IndexStatus.READY)
        self.assertEqual(index.source_version_id, self.original.version.version_id)
        self.assertEqual(index.inventory.unit_digest, self.original.version.chunk_digest)
        self.assertIsNone(index.artifact_ref)  # Legacy data remains in its existing stores.

    def test_graphmind_jobs_build_retry_and_delete_through_the_shared_lifecycle(self):
        document_id = self.original.document.document_id
        previous = self.app.indexes.ready(document_id, "graphmind")
        self.path.write_text("A replacement source with a supported fact.", encoding="utf-8")
        submission = self.app.ingestion.prepare_import(self.path)
        recipe = legacy_graphmind_recipe(submission.version)
        index_id = recipe.index_id(self.app.installation_id, submission.version.version_id)
        self.assertEqual(self.app.indexes.index(index_id).status, IndexStatus.STAGED)

        def fail(point):
            if point == "after_vector":
                self.assertEqual(self.app.indexes.index(index_id).status, IndexStatus.BUILDING)
                raise PublicationError("injected build failure")

        self.app.ingestion.failure_injector = fail
        with self.assertRaises(PublicationError):
            self.app.jobs.run_job(submission.job.job_id)
        self.assertEqual(self.app.indexes.index(index_id).status, IndexStatus.FAILED)
        self.assertEqual(self.app.indexes.ready(document_id, "graphmind").index_id, previous.index_id)
        self.app.ingestion.failure_injector = None
        self.app.jobs.run_job(submission.job.job_id)
        self.assertEqual(self.app.indexes.ready(document_id, "graphmind").index_id, index_id)
        # An explicit derived-index job uses the same executor, stores and source.
        rebuild = self.app.indexes.submit(submission.version.version_id, recipe, max_queued_jobs=10)
        self.app.jobs.run_job(rebuild.job_id)
        self.assertEqual(self.app.metadata.job(rebuild.job_id).status, JobStatus.SUCCEEDED)
        deletion = self.app.ingestion.prepare_delete(document_id)
        self.app.jobs.run_job(deletion.job.job_id)
        self.assertEqual(self.app.indexes.index(index_id).status, IndexStatus.INVALIDATED)
        self.assertIsNone(self.app.indexes.ready(document_id, "graphmind"))

    def test_new_recipe_attaches_to_same_source_and_failure_preserves_old_head(self):
        job, index_id = self.request()
        self.complete(job, index_id, activate=False)
        self.assertIsNone(self.indexes.ready(job.document_id, "test_engine"))
        self.complete(job, index_id)
        self.finish(job)
        replacement, other_id = self.request("v2")
        self.assertNotEqual(index_id, other_id)
        self.indexes.start(replacement, other_id)
        self.indexes.fail(replacement, other_id, "model_unavailable")
        self.assertEqual(self.indexes.index(other_id).status, IndexStatus.FAILED)
        self.assertEqual(self.indexes.ready(job.document_id, "test_engine").index_id, index_id)
        self.assertEqual(self.app.metadata.document(job.document_id).active_version_id, job.version_id)
        self.assertEqual((self.app.settings.data_dir / self.original.version.private_file_ref).read_bytes(),
                         self.path.read_bytes())
        self.finish(replacement, succeeded=False)

    def test_ready_inventory_is_immutable_and_after_publish_failure_does_not_downgrade_it(self):
        job, index_id = self.request()
        self.complete(job, index_id)
        self.indexes.fail(job, index_id, "post_publish_failure")
        self.assertEqual(self.indexes.index(index_id).status, IndexStatus.READY)
        with self.assertRaises(PublicationError):
            self.indexes.complete(job, index_id, IndexInventory.from_unit_ids(["different"]),
                                  artifact_ref=f"derived/{index_id}/manifest.json", artifact_digest="0" * 64)
        self.finish(job)

    def test_attempt_fence_binds_source_operation_and_immutable_input(self):
        job, index_id = self.request()
        for forged in (replace(job, document_id="other"), replace(job, version_id="other"),
                       replace(job, operation="delete_document"), replace(job, payload_json="{}"),
                       replace(job, attempt_count=job.attempt_count + 1)):
            with self.subTest(forged=forged), self.assertRaises(JobLeaseError):
                self.indexes.start(forged, index_id)
        self.finish(job)
        with self.assertRaises(JobLeaseError):
            self.indexes.start(job, index_id)

    def test_tombstone_immediately_hides_heads_and_cleanup_invalidates_every_engine(self):
        job, index_id = self.request()
        self.complete(job, index_id)
        self.finish(job)
        deletion = self.app.ingestion.prepare_delete(job.document_id)
        self.assertIsNone(self.indexes.ready(job.document_id, "test_engine"))
        self.assertIsNone(self.indexes.ready(job.document_id, "graphmind"))
        cleanup = self.claim(deletion.job)
        self.indexes.invalidate_document(cleanup)
        self.assertEqual(self.indexes.index(index_id).status, IndexStatus.INVALIDATED)
        self.finish(cleanup)

    def test_job_input_roundtrip_and_idempotency_reject_target_changes(self):
        job, index_id = self.request()
        self.assertEqual(self.app.metadata.job(job.job_id).payload_json, job.payload_json)
        with self.assertRaises(JobInputError):
            self.app.metadata.put_job(replace(job, payload_json='{"different":true}'))
        with self.assertRaises(JobInputError):
            self.app.metadata.put_job(replace(job, idempotency_key="invalid-input", payload_json="[]"))
        self.finish(job)

    def test_v2_upgrade_preserves_sources_jobs_and_legacy_heads_without_opening_files(self):
        before = self.indexes.ready(self.original.document.document_id, "graphmind")
        installation = self.app.metadata.installation_id()
        with self.app.metadata.transaction() as connection:
            connection.execute("DROP TABLE engine_index_heads")
            connection.execute("DROP TABLE engine_indexes")
            connection.execute("ALTER TABLE jobs DROP COLUMN payload_json")
            connection.execute("PRAGMA user_version=2")
        store = MetadataStore(self.app.metadata.path)
        with patch("graphmind.files.PrivateFileStore.read_bytes", side_effect=AssertionError("No original read")):
            store.migrate()
        after = IndexRepository(store).ready(self.original.document.document_id, "graphmind")
        self.assertEqual(after.index_id, before.index_id)
        self.assertEqual(after.inventory, before.inventory)
        self.assertEqual(store.installation_id(), installation)
        self.assertEqual(store.document(self.original.document.document_id).active_version_id,
                         self.original.version.version_id)
        self.assertEqual(store.job(self.original.job.job_id).payload_json, "{}")

    def test_source_and_selected_heads_publish_atomically_after_all_indexes_are_ready(self):
        document_id = self.original.document.document_id
        version_id = str(uuid.uuid4())
        body = b"Replacement original source."
        chunk = Chunk("replacement-unit", version_id, document_id, 0, body.decode(), "line 1", 0, len(body))
        inventory = IndexInventory.from_unit_ids([chunk.chunk_id])
        reference = self.app.files.put_bytes(document_id, version_id, body, ".txt")
        version = replace(self.original.version, version_id=version_id, content_hash=hashlib.sha256(body).hexdigest(),
                          private_file_ref=reference, source_size=len(body), expected_chunk_count=1,
                          chunk_digest=inventory.unit_digest, status=VersionStatus.STAGED)
        self.app.metadata.put_version(version)
        self.app.metadata.replace_chunks(version_id, [chunk])
        targets = [self.indexes.prepare(version_id, IndexRecipe.create(engine, "v1", {}))
                   for engine in ("test_a", "test_b")]
        key = "test-multi:" + version_id
        queued = self.app.metadata.put_job(Job(str(uuid.uuid4()), key, "import_document", document_id,
            version_id, JobStatus.QUEUED, 0, utc_now(), payload_json=json.dumps(
                {"schema_version": 1, "index_ids": [item.index_id for item in targets]})))
        job = self.claim(queued)
        self.complete(job, targets[0].index_id, activate=False)
        self.indexes.start(job, targets[1].index_id)
        with self.assertRaises(PublicationError):
            self.app.metadata.publish_version(version_id, job=job)
        self.assertEqual(self.app.metadata.document(document_id).active_version_id,
                         self.original.version.version_id)
        self.assertIsNone(self.indexes.ready(document_id, "test_a"))
        self.complete(job, targets[1].index_id, activate=False)
        self.app.metadata.publish_version(version_id, job=job)
        self.assertEqual(self.app.metadata.document(document_id).active_version_id, version_id)
        self.assertTrue(all(self.indexes.ready(document_id, engine) for engine in ("test_a", "test_b")))
        self.assertIsNone(self.indexes.ready(document_id, "graphmind"))
        self.finish(job)
