"""Continuous worker uses durable failures/publication and graceful shutdown."""
from pathlib import Path
import tempfile
import threading
import unittest

from graphmind.domain import JobStatus
from graphmind.errors import PublicationError
from tests.support.application import make_app


class WorkerServiceTests(unittest.TestCase):
    def test_predecessor_lease_expiring_after_startup_is_reconciled(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); stop = threading.Event()
            with make_app(root, failure_injector=lambda point: stop.set() if point == 'after_publish' else None) as app:
                source = root/'owned.txt'; source.write_text('Owned code is CERULEAN.')
                submission = app.ingestion.prepare_import(source)
                self.assertTrue(app.metadata.acquire_writer('crashed-process', 60))
                app.metadata.claim_next_job('crashed-process', 60, job_id=submission.job.job_id)
                app.metadata.release_writer('crashed-process')
                case = self
                class ExpiringLease:
                    def is_set(self): return stop.is_set()
                    def wait(self, seconds):
                        case.assertEqual(app.metadata.job(submission.job.job_id).status, JobStatus.RUNNING)
                        with app.metadata.transaction() as connection:
                            connection.execute('UPDATE jobs SET lease_expires_at=? WHERE job_id=?',
                                               ('2000-01-01T00:00:00+00:00', submission.job.job_id))
                app.jobs.run_forever(ExpiringLease(), poll_seconds=1)
                completed = app.metadata.job(submission.job.job_id)
                self.assertEqual((completed.status, completed.attempt_count), (JobStatus.SUCCEEDED, 2))
                self.assertEqual(app.metadata.document(completed.document_id).active_version_id, completed.version_id)

    def test_failed_file_does_not_block_next_job_and_stop_finishes_publication(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); stop = threading.Event(); injected = False
            def failure(point):
                nonlocal injected
                if point == 'after_vector' and not injected:
                    injected = True
                    raise PublicationError('private source body must not be logged')
                if point == 'after_publish':
                    stop.set()
            with make_app(root, failure_injector=failure) as app:
                submissions = []
                for name in ('first.txt', 'second.txt'):
                    source = root/name; source.write_text('Owned code is CERULEAN.')
                    submissions.append(app.ingestion.prepare_import(source))
                with self.assertLogs('graphmind.jobs', level='WARNING') as logs:
                    app.jobs.run_forever(stop, poll_seconds=1)
                first, second = [app.metadata.job(item.job.job_id) for item in submissions]
                self.assertEqual(first.status, JobStatus.FAILED)
                self.assertEqual(first.error_code, 'publication_error')
                self.assertEqual(second.status, JobStatus.SUCCEEDED)
                self.assertEqual(app.metadata.document(second.document_id).active_version_id, second.version_id)
                self.assertNotIn('private source body', '\n'.join(logs.output))

    def test_stop_interrupts_idle_wait_without_claiming_more_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            with make_app(Path(temporary)) as app:
                stop = threading.Event()
                entered_wait = threading.Event()
                class IdleStop:
                    def is_set(self): return stop.is_set()
                    def wait(self, seconds):
                        entered_wait.set()
                        return stop.wait(seconds)
                thread = threading.Thread(target=app.jobs.run_forever, args=(IdleStop(),),
                                          kwargs={'poll_seconds': 60})
                thread.start()
                try:
                    self.assertTrue(entered_wait.wait(3))
                    stop.set()
                    thread.join(3)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(app.metadata.list_jobs(), [])
                finally:
                    stop.set(); thread.join(3)
