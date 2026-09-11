from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from graphmind.config import Settings
from graphmind.domain import Chunk, Document, Job, JobStatus, Version, VersionStatus
from graphmind.errors import (
    ConfigurationError,
    JobLeaseError,
    JobQueueFullError,
    MigrationBusyError,
    UnsupportedSchemaError,
)
from graphmind.jobs import DurableJobExecutor
from graphmind.metadata import CURRENT_SCHEMA_VERSION, MetadataStore, _MIGRATION_1, utc_now


def make_settings(root: Path, **overrides) -> Settings:
    values = {
        "data_dir": root / "data",
        "allowed_import_roots": (root,),
        "neo4j_password": "",
    }
    values.update(overrides)
    return Settings.load(
        dotenv_path=root / "missing.env",
        environ={},
        explicit=values,
    )


def seed_contracts(store: MetadataStore) -> tuple[Document, Version, Chunk]:
    document_id = str(uuid.uuid4())
    version_id = str(uuid.uuid4())
    document = Document(
        document_id=document_id,
        collection_id="default",
        display_name="fixture.txt",
        normalized_name="fixture.txt",
        media_type="text/plain",
        source_key=str(uuid.uuid4()),
        created_at=utc_now(),
    )
    version = Version(
        version_id=version_id,
        document_id=document_id,
        content_hash="content",
        extractor_fingerprint="extractor",
        chunker_fingerprint="chunker",
        embedding_fingerprint="embedding",
        private_file_ref="files/fixture.txt",
        source_size=7,
        expected_chunk_count=1,
        chunk_digest="digest",
        status=VersionStatus.STAGED,
        created_at=utc_now(),
    )
    chunk = Chunk(
        chunk_id=str(uuid.uuid4()),
        version_id=version_id,
        document_id=document_id,
        ordinal=0,
        text="fixture",
        locator="line 1",
        start_offset=0,
        end_offset=7,
    )
    store.put_document(document)
    store.put_version(version)
    store.replace_chunks(version_id, [chunk])
    return document, version, chunk


def make_job(document: Document, version: Version, key: str = "fixture") -> Job:
    return Job(
        job_id=str(uuid.uuid4()),
        idempotency_key=key,
        operation="import_txt",
        document_id=document.document_id,
        version_id=version.version_id,
        status=JobStatus.QUEUED,
        attempt_count=0,
        available_at=utc_now(),
    )


class SettingsTests(unittest.TestCase):
    def test_precedence_unicode_paths_and_redaction(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_dir = root / "configuration Ω"
            config_dir.mkdir()
            config_path = config_dir / "graphmind.toml"
            config_path.write_text(
                """
[graphmind]
data_dir = "runtime with spaces"
allowed_import_roots = ["imports Ω"]
chunk_size = 500
neo4j_password = "toml-secret"
""".strip(),
                encoding="utf-8",
            )
            dotenv_path = root / ".env"
            dotenv_path.write_text("GRAPHMIND_CHUNK_SIZE=600\nNEO4J_PASSWORD=dotenv-secret\n", encoding="utf-8")
            settings = Settings.load(
                config_path=config_path,
                dotenv_path=dotenv_path,
                environ={"GRAPHMIND_CHUNK_SIZE": "700", "NEO4J_PASSWORD": "env-secret"},
                explicit={"chunk_size": 800},
            )
            self.assertEqual(settings.chunk_size, 800)
            self.assertEqual(settings.data_dir, (config_dir / "runtime with spaces").resolve())
            self.assertEqual(settings.allowed_import_roots, ((config_dir / "imports Ω").resolve(),))
            diagnostics = json.dumps(settings.diagnostics())
            self.assertNotIn("env-secret", diagnostics)
            self.assertIn("<configured>", diagnostics)
            settings.ensure_private_paths()
            self.assertTrue(settings.sqlite_path.parent.is_dir())

    def test_invalid_settings_and_missing_secrets_fail_clearly(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(ConfigurationError):
                make_settings(root, chunk_size=100, chunk_overlap=100)
            with self.assertRaisesRegex(ConfigurationError, "OLLAMA_BASE_URL"):
                make_settings(root, ollama_base_url="localhost:11434")
            with self.assertRaisesRegex(ConfigurationError, "ANSWER_PROVIDER"):
                make_settings(root, answer_provider="unknown")
            with self.assertRaisesRegex(ConfigurationError, "ANSWER_MODEL"):
                make_settings(root, answer_model=" ")
            settings = make_settings(root)
            with self.assertRaisesRegex(ConfigurationError, "NEO4J_PASSWORD"):
                settings.validate(require_neo4j_secret=True)
            with self.assertRaisesRegex(ConfigurationError, "OLLAMA_API_KEY"):
                settings.validate(require_provider_secret=True)

    def test_path_permission_failure_is_typed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            blocker = root / "not-a-directory"
            blocker.write_text("x", encoding="utf-8")
            settings = make_settings(root, data_dir=blocker)
            with self.assertRaises(ConfigurationError):
                settings.ensure_private_paths()


class MigrationTests(unittest.TestCase):
    def test_fresh_and_v1_upgrade_preserve_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "metadata.sqlite3"
            connection = sqlite3.connect(path)
            connection.executescript(_MIGRATION_1)
            connection.execute("PRAGMA user_version = 1")
            connection.execute(
                """
                INSERT INTO documents(
                    document_id, collection_id, display_name, normalized_name,
                    media_type, source_key, created_at
                ) VALUES ('d1', 'default', 'kept.txt', 'kept.txt', 'text/plain', 'source', 'now')
                """
            )
            connection.commit()
            connection.close()

            store = MetadataStore(path)
            store.migrate()
            self.assertEqual(store.schema_version(), CURRENT_SCHEMA_VERSION)
            self.assertEqual(store.document("d1").display_name, "kept.txt")
            with store.connection() as migrated:
                columns = {row[1] for row in migrated.execute("PRAGMA table_info(versions)")}
            self.assertIn("source_size", columns)

    def test_future_schema_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "metadata.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute("PRAGMA user_version = 999")
            connection.close()
            with self.assertRaises(UnsupportedSchemaError):
                MetadataStore(path).migrate()

    def test_concurrent_migration_lock_is_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "metadata.sqlite3"
            store = MetadataStore(path)
            store.migrate()
            lock = sqlite3.connect(path, isolation_level=None)
            lock.execute("BEGIN EXCLUSIVE")
            try:
                with self.assertRaises(MigrationBusyError):
                    MetadataStore(path, busy_timeout_ms=20).migrate()
            finally:
                lock.rollback()
                lock.close()


class DurableJobTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = MetadataStore(Path(self.temporary.name) / "metadata.sqlite3")
        self.store.migrate()
        self.document, self.version, _ = seed_contracts(self.store)

    def tearDown(self):
        self.temporary.cleanup()

    def test_idempotent_concurrent_submission_and_single_writer(self):
        def submit(_: int) -> str:
            return self.store.put_job(make_job(self.document, self.version, "same-key")).job_id

        with ThreadPoolExecutor(max_workers=2) as pool:
            job_ids = list(pool.map(submit, range(2)))
        self.assertEqual(job_ids[0], job_ids[1])
        self.assertEqual(len(self.store.list_jobs()), 1)
        self.assertTrue(self.store.acquire_writer("writer-a", 30))
        self.assertFalse(self.store.acquire_writer("writer-b", 30))
        self.store.release_writer("writer-a")

    def test_executor_success_and_competing_writer_error(self):
        job = self.store.put_job(make_job(self.document, self.version))
        handled: list[str] = []
        executor = DurableJobExecutor(
            self.store,
            lambda item: handled.append(item.job_id),
            lease_seconds=30,
            owner="worker",
        )
        completed = executor.run_once()
        self.assertEqual(handled, [job.job_id])
        self.assertEqual(completed.status, JobStatus.SUCCEEDED)

        second = self.store.put_job(make_job(self.document, self.version, "second"))
        self.assertTrue(self.store.acquire_writer("other", 30))
        try:
            with self.assertRaises(JobLeaseError):
                executor.run_once()
        finally:
            self.store.release_writer("other")
        self.assertEqual(self.store.job(second.job_id).status, JobStatus.QUEUED)

    def test_expired_running_job_is_reconciled(self):
        job = self.store.put_job(make_job(self.document, self.version))
        self.assertTrue(self.store.acquire_writer("crashed", 30))
        claimed = self.store.claim_next_job("crashed", 30)
        self.assertEqual(claimed.job_id, job.job_id)
        self.assertTrue(self.store.heartbeat(job.job_id, "crashed", 30))
        self.assertFalse(self.store.heartbeat(job.job_id, "different-writer", 30))
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE jobs SET lease_expires_at = '2000-01-01T00:00:00+00:00' WHERE job_id = ?",
                (job.job_id,),
            )
            connection.execute(
                "UPDATE writer_lock SET lease_expires_at = '2000-01-01T00:00:00+00:00' WHERE singleton = 1"
            )
        self.assertEqual(self.store.reconcile(), 1)
        recovered = self.store.job(job.job_id)
        self.assertEqual(recovered.status, JobStatus.QUEUED)
        self.assertEqual(recovered.error_code, "interrupted")

    def test_queue_capacity_is_enforced_without_breaking_idempotency(self):
        first = self.store.put_job(
            make_job(self.document, self.version, "first"), max_queued_jobs=1
        )
        duplicate = self.store.put_job(
            make_job(self.document, self.version, "first"), max_queued_jobs=1
        )
        self.assertEqual(duplicate.job_id, first.job_id)
        with self.assertRaises(JobQueueFullError):
            self.store.put_job(
                make_job(self.document, self.version, "second"), max_queued_jobs=1
            )


if __name__ == "__main__":
    unittest.main()
