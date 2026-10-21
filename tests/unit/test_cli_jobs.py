"""Submitted-job completion against disposable state, without external services."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from graphmind import cli
from graphmind.domain import JobStatus
from graphmind.errors import GraphMindError, JobLeaseError, PublicationError
from tests.support.application import make_app


class SubmittedJobTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.app = make_app(self.root)
        self.addCleanup(self.app.close)

    def source(self, name, text="A synthetic source contains the launch code CERULEAN."):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def invoke(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        code = 0
        with patch.object(cli, "_settings", return_value=self.app.settings), patch.object(
            cli, "GraphMindApplication", return_value=self.app
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                cli.main(list(arguments))
            except SystemExit as exc:
                code = exc.code
        return (
            code,
            json.loads(stdout.getvalue()) if stdout.getvalue() else None,
            json.loads(stderr.getvalue()) if stderr.getvalue() else None,
        )

    def test_immediate_import_and_reindex_complete_the_requested_job_only(self):
        for command in ("import", "reindex"):
            with self.subTest(command=command):
                target = self.source(f"{command}.txt")
                previous_version = None
                if command == "reindex":
                    previous = self.app.ingestion.prepare_import(target)
                    self.app.jobs.run_once()
                    previous_version = previous.version.version_id
                    target.write_text("The replacement launch code is AMBER.", encoding="utf-8")
                older = self.app.ingestion.prepare_import(self.source(f"older-{command}.txt"))
                code, output, error = self.invoke(command, str(target))
                requested = self.app.ingestion.prepare_import(target)
                self.assertEqual(code, 0, error)
                self.assertEqual(self.app.metadata.job(requested.job.job_id).status, JobStatus.SUCCEEDED)
                self.assertEqual(self.app.metadata.job(older.job.job_id).status, JobStatus.QUEUED)
                self.assertEqual(output["submitted_job"]["job_id"], requested.job.job_id)
                self.assertEqual(output["submitted_job"]["status"], "succeeded")
                self.assertEqual(output["submitted_job"]["progress"], 100)
                self.assertIn("warnings", output)
                self.assertEqual(self.app.metadata.document(requested.document.document_id).active_version_id,
                                 requested.version.version_id)
                if previous_version is not None:
                    self.assertNotEqual(previous_version, requested.version.version_id)
                # Finish this subtest's unrelated backlog through the normal worker.
                self.app.jobs.run_once()

    def test_delete_completes_its_cleanup_without_processing_older_import(self):
        target = self.app.ingestion.prepare_import(self.source("deleted.txt"))
        self.app.jobs.run_once()
        older = self.app.ingestion.prepare_import(self.source("older.txt"))
        code, output, error = self.invoke("delete", target.document.document_id)
        self.assertEqual(code, 0, error)
        self.assertEqual(output["submitted_job"]["status"], "succeeded")
        self.assertEqual(self.app.metadata.job(older.job.job_id).status, JobStatus.QUEUED)
        self.assertIsNotNone(self.app.metadata.document(target.document.document_id).deleted_at)
        self.assertEqual(self.app.vector.chunk_ids_for_version(self.app.installation_id,
                                                              target.version.version_id), set())
        self.assertFalse((self.app.settings.files_dir / target.document.document_id).exists())

    def test_target_cannot_overtake_an_older_pending_version_of_the_same_document(self):
        path = self.source("replacement.txt", "The initial launch code is CERULEAN.")
        older = self.app.ingestion.prepare_import(path)
        path.write_text("The replacement launch code is AMBER.", encoding="utf-8")
        code, output, error = self.invoke("reindex", str(path))
        self.assertEqual(code, 2)
        self.assertEqual(error["error"], "job_not_ready")
        self.assertEqual(output["submitted_job"]["status"], "queued")
        self.assertEqual(self.app.metadata.job(older.job.job_id).status, JobStatus.QUEUED)
        self.assertIsNone(self.app.metadata.document(older.document.document_id).active_version_id)
        self.assertEqual(self.app.jobs.run_once().job_id, older.job.job_id)
        code, output, error = self.invoke("reindex", str(path))
        self.assertEqual(code, 0, error)
        latest_version = output["submitted_job"]["version_id"]
        self.assertNotEqual(latest_version, older.version.version_id)
        self.assertEqual(self.app.metadata.document(older.document.document_id).active_version_id,
                         latest_version)
        self.assertIsNone(self.app.jobs.run_once())
        self.assertEqual(self.app.metadata.document(older.document.document_id).active_version_id,
                         latest_version)

    def test_enqueue_only_reports_actual_job_without_execution(self):
        for command in ("import", "reindex"):
            with self.subTest(command=command):
                code, output, error = self.invoke(command, str(self.source(f"{command}.txt")), "--enqueue-only")
                self.assertEqual(code, 0, error)
                self.assertEqual(output["submitted_job"]["status"], "queued")
                self.assertEqual(output["submitted_job"]["attempt_count"], 0)
        target = self.app.ingestion.prepare_import(self.source("delete.txt"))
        code, output, error = self.invoke("delete", target.document.document_id, "--enqueue-only")
        self.assertEqual(code, 0, error)
        self.assertEqual(output["submitted_job"]["status"], "queued")
        self.assertTrue(all(job.attempt_count == 0 for job in self.app.metadata.list_jobs()))

    def test_completed_import_is_idempotent_even_when_writer_is_busy(self):
        path = self.source("complete.txt")
        target = self.app.ingestion.prepare_import(path)
        self.app.jobs.run_once()
        older = self.app.ingestion.prepare_import(self.source("older.txt"))
        self.assertTrue(self.app.metadata.acquire_writer("other", 60))
        try:
            code, output, error = self.invoke("import", str(path))
        finally:
            self.app.metadata.release_writer("other")
        self.assertEqual(code, 0, error)
        self.assertEqual(output["submitted_job"]["attempt_count"], 1)
        self.assertEqual(self.app.metadata.job(target.job.job_id).status, JobStatus.SUCCEEDED)
        self.assertEqual(self.app.metadata.job(older.job.job_id).status, JobStatus.QUEUED)

    def fail_after_vector(self, point):
        if point == "after_vector":
            raise PublicationError("Synthetic publication failure")

    def test_failed_job_is_reported_and_can_be_retried_without_draining_queue(self):
        path = self.source("retry.txt")
        self.app.ingestion.failure_injector = self.fail_after_vector
        code, output, error = self.invoke("import", str(path))
        self.assertEqual(code, 2)
        self.assertEqual(error["error"], PublicationError.code)
        self.assertEqual(output["submitted_job"]["status"], "failed")
        self.assertEqual(output["submitted_job"]["error_code"], PublicationError.code)
        document = self.app.metadata.document(output["submitted_job"]["document_id"])
        self.assertIsNone(document.active_version_id)
        older = self.app.ingestion.prepare_import(self.source("older.txt"))
        self.app.ingestion.failure_injector = None
        code, output, error = self.invoke("import", str(path))
        self.assertEqual(code, 0, error)
        self.assertEqual(output["submitted_job"]["status"], "succeeded")
        self.assertEqual(output["submitted_job"]["attempt_count"], 2)
        self.assertEqual(self.app.metadata.job(older.job.job_id).status, JobStatus.QUEUED)

    def test_busy_writer_reports_pending_target_and_nonzero_exit(self):
        self.assertTrue(self.app.metadata.acquire_writer("other", 60))
        try:
            code, output, error = self.invoke("import", str(self.source("busy.txt")))
        finally:
            self.app.metadata.release_writer("other")
        self.assertEqual(code, 2)
        self.assertEqual(error["error"], JobLeaseError.code)
        self.assertEqual(output["submitted_job"]["status"], "queued")
        self.assertEqual(output["submitted_job"]["attempt_count"], 0)

    def test_target_recovery_respects_live_lease_then_reconciles_expired_attempt(self):
        target = self.app.ingestion.prepare_import(self.source("interrupted.txt"))
        self.app.metadata.acquire_writer("crashed", 60)
        self.app.metadata.claim_next_job("crashed", 60)
        with self.assertRaises(JobLeaseError):
            self.app.jobs.run_job(target.job.job_id)
        with self.app.metadata.transaction() as connection:
            connection.execute("UPDATE jobs SET lease_expires_at = '2000-01-01'")
            connection.execute("UPDATE writer_lock SET lease_expires_at = '2000-01-01'")
        completed = self.app.jobs.run_job(target.job.job_id)
        self.assertEqual(completed.status, JobStatus.SUCCEEDED)
        self.assertEqual(completed.attempt_count, 2)

    def test_missing_and_delayed_targets_never_run_an_unrelated_job(self):
        first = self.app.ingestion.prepare_import(self.source("first.txt"))
        with self.assertRaises(GraphMindError) as missing:
            self.app.jobs.run_job("missing-job")
        self.assertEqual(missing.exception.code, "job_not_found")
        with self.app.metadata.transaction() as connection:
            connection.execute("UPDATE jobs SET available_at = '2999-01-01' WHERE job_id = ?",
                               (first.job.job_id,))
        second = self.app.ingestion.prepare_import(self.source("second.txt"))
        with self.assertRaises(GraphMindError) as delayed:
            self.app.jobs.run_job(first.job.job_id)
        self.assertEqual(delayed.exception.code, "job_not_ready")
        self.assertEqual(self.app.metadata.job(second.job.job_id).status, JobStatus.QUEUED)

    def test_target_execution_keeps_kernel_lock_and_preserves_worker_fifo(self):
        first = self.app.ingestion.prepare_import(self.source("first.txt"))
        second = self.app.ingestion.prepare_import(self.source("second.txt"))
        original_handler = self.app.jobs.handler

        def handler(job):
            from graphmind.jobs import DurableJobExecutor
            competitor = DurableJobExecutor(self.app.metadata, original_handler, lease_seconds=10)
            with self.assertRaises(JobLeaseError):
                competitor.run_job(second.job.job_id)
            original_handler(job)

        self.app.jobs.handler = handler
        self.assertEqual(self.app.jobs.run_job(first.job.job_id).status, JobStatus.SUCCEEDED)
        self.app.jobs.handler = original_handler
        third = self.app.ingestion.prepare_import(self.source("third.txt"))
        self.assertEqual(self.app.jobs.run_once().job_id, second.job.job_id)
        self.assertEqual(self.app.metadata.job(third.job.job_id).status, JobStatus.QUEUED)

    def test_unexpected_handler_failure_is_sanitized_and_retains_job_failure(self):
        private_marker = "synthetic-private-detail-must-not-be-printed"

        def handler(job):
            raise RuntimeError(private_marker)

        self.app.jobs.handler = handler
        with self.assertLogs("graphmind.jobs", level="ERROR") as logs:
            code, output, error = self.invoke("import", str(self.source("unexpected.txt")))
        self.assertEqual(code, 2)
        self.assertEqual(error["error"], "job_execution_error")
        self.assertEqual(output["submitted_job"]["status"], "failed")
        self.assertEqual(output["submitted_job"]["error_code"], "internal_error")
        self.assertNotIn(private_marker, json.dumps((output, error)))
        self.assertNotIn(private_marker, "\n".join(logs.output))

    def test_retry_unknown_job_returns_typed_error(self):
        code, output, error = self.invoke("retry", "missing-job")
        self.assertEqual(code, 2)
        self.assertEqual(error["error"], "job_not_found")
        self.assertIsNone(output)

    def test_evaluation_import_uses_the_same_targeted_execution(self):
        older = self.app.ingestion.prepare_import(self.source("older.txt"))
        path = self.source("evaluation.txt")
        materialized = SimpleNamespace(root=self.root, document_paths=(path,))
        dataset = SimpleNamespace(materialize=lambda root: materialized)
        with patch.object(cli, "FrozenEvaluationDataset", return_value=dataset), patch.object(
            cli, "EvaluationRunner"
        ) as runner:
            runner.return_value.run.return_value = {"summary": {"gate_passed": True}}
            code, _, error = self.invoke("evaluate", "--fixture", "unused", "--source", "unused",
                                         "--work-dir", str(self.root), "--report", str(self.root / "report.json"),
                                         "--label", "synthetic", "--extractive")
        self.assertEqual(code, 0, error)
        self.assertEqual(self.app.metadata.job(older.job.job_id).status, JobStatus.QUEUED)
        self.assertEqual(self.app.ingestion.prepare_import(path).job.status, JobStatus.SUCCEEDED)


if __name__ == "__main__":
    unittest.main()
