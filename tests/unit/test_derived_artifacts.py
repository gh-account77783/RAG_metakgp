"""Private derived artifacts authenticate their source, recipe and file closure."""
import tempfile
from pathlib import Path
import unittest

from graphmind.errors import JobLeaseError, ManifestUnavailableError, PublicationError
from graphmind.rag.components.artifacts import DerivedArtifactStore
from graphmind.rag.components.index_contracts import IndexRecipe
from tests.unit.test_ingestion_lifecycle import make_app, run_submission


class DerivedArtifactTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        path = self.root / "source.txt"
        path.write_text("Original source for the artifact.", encoding="utf-8")
        self.app = make_app(self.root)
        self.addCleanup(self.app.close)
        self.source = run_submission(self.app, path)
        recipe = IndexRecipe.create("test_engine", "artifact-v1", {})
        queued = self.app.indexes.submit(self.source.version.version_id, recipe, max_queued_jobs=10)
        self.assertTrue(self.app.metadata.acquire_writer("artifact-test", 30))
        self.job = self.app.metadata.claim_next_job("artifact-test", 30, job_id=queued.job_id)
        self.index_id = recipe.index_id(self.app.installation_id, self.job.version_id)
        self.app.indexes.start(self.job, self.index_id)
        self.store = DerivedArtifactStore(self.app.settings.data_dir, self.app.indexes)

    def publish(self):
        receipt = self.store.publish(self.job, self.index_id, ["page:1"], {"pages.json": b'{"page":1}'})
        self.app.indexes.complete(self.job, self.index_id, receipt.inventory, activate=True,
                                  artifact_ref=receipt.reference, artifact_digest=receipt.digest)
        return self.app.indexes.index(self.index_id)

    def finish(self):
        self.app.metadata.finish_job(self.job.job_id, "artifact-test", succeeded=True, job=self.job)
        self.app.metadata.release_writer("artifact-test")

    def test_roundtrip_restart_and_idempotent_publish_preserve_original(self):
        index = self.publish()
        restarted = DerivedArtifactStore(self.app.settings.data_dir, self.app.indexes)
        self.assertEqual(restarted.read(index), {"pages.json": b'{"page":1}'})
        self.assertEqual(restarted.publish(self.job, self.index_id, ["page:1"],
                                          {"pages.json": b'{"page":1}'}).digest, index.artifact_digest)
        with self.assertRaises(PublicationError):
            restarted.publish(self.job, self.index_id, ["page:1"], {"pages.json": b"different"})
        self.assertEqual((self.app.settings.data_dir / self.source.version.private_file_ref).read_text(),
                         "Original source for the artifact.")
        self.finish()
        with self.assertRaises(JobLeaseError):
            restarted.publish(self.job, self.index_id, ["page:1"], {"pages.json": b"late"})

    def test_tampered_content_or_unlisted_file_is_rejected(self):
        index = self.publish()
        directory = self.store.root / self.index_id
        (directory / "pages.json").write_bytes(b'{"page":9}')
        with self.assertRaises(ManifestUnavailableError):
            self.store.read(index)
        (directory / "pages.json").write_bytes(b'{"page":1}')
        (directory / "unlisted.json").write_bytes(b"extra")
        with self.assertRaises(ManifestUnavailableError):
            self.store.read(index)
        self.finish()

    def test_oversized_build_stays_unpublished_and_delete_cleans_only_derived_directory(self):
        limited = DerivedArtifactStore(self.app.settings.data_dir, self.app.indexes, max_bytes=2)
        with self.assertRaises(PublicationError):
            limited.publish(self.job, self.index_id, ["page:1"], {"pages.json": b"large"})
        self.assertIsNone(self.app.indexes.ready(self.job.document_id, "test_engine"))
        self.publish()
        self.finish()
        deletion = self.app.ingestion.prepare_delete(self.job.document_id)
        self.assertTrue(self.app.metadata.acquire_writer("artifact-delete", 30))
        job = self.app.metadata.claim_next_job("artifact-delete", 30, job_id=deletion.job.job_id)
        self.store.delete_document(job)
        self.assertFalse((self.store.root / self.index_id).exists())
        self.assertTrue((self.app.settings.data_dir / self.source.version.private_file_ref).is_file())
        self.app.metadata.finish_job(job.job_id, "artifact-delete", succeeded=True, job=job)
        self.app.metadata.release_writer("artifact-delete")
