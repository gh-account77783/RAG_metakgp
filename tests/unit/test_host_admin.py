"""Owned file/folder workflows reuse the actual source/job lifecycle."""
import io
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from graphmind.cli import _job_status, _status
from graphmind.domain import JobStatus
from graphmind.errors import PathValidationError
from graphmind.host_admin import folder_sources, import_sources, wizard
from tests.support.application import make_app


class HostAdministrationTests(unittest.TestCase):
    def test_recursive_selection_is_bounded_before_mutation_and_skips_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'one.txt').write_text('Owned one.')
            child = root / 'child'; child.mkdir()
            (child / 'two.md').write_text('# Owned two')
            (root / 'ignored.bin').write_bytes(b'owned')
            with make_app(root) as app:
                (app.settings.data_dir / 'not-an-input.txt').write_text('Runtime data.')
                self.assertEqual(folder_sources(app, root), [root / 'one.txt'])
                with self.assertRaises(PathValidationError):
                    folder_sources(app, root, recursive=True, max_files=1)
                self.assertEqual(app.metadata.list_jobs(), [])
                self.assertEqual(len(folder_sources(app, root, recursive=True)), 2)
                with self.assertRaises(PathValidationError):
                    folder_sources(app, app.settings.data_dir)

    def test_file_error_does_not_hide_other_completed_engine_states(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'empty.txt').write_text('')
            (root / 'valid.txt').write_text('The owned launch code is CERULEAN.')
            with make_app(root) as app:
                result = import_sources(app, folder_sources(app, root), job_status=_job_status)
                self.assertEqual(result['failed_files'], 1)
                self.assertEqual(result['files'][0]['error'], 'empty_document')
                ready = result['files'][1]
                self.assertEqual(ready['submitted_job']['status'], 'succeeded')
                self.assertTrue(ready['indexes'][0]['active'])

    def test_enqueue_uses_the_existing_queue_without_running_a_job(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source = root / 'owned.txt'; source.write_text('Owned source.')
            with make_app(root) as app:
                result = import_sources(app, [source], enqueue_only=True, job_status=_job_status)
                self.assertEqual(result['files'][0]['submitted_job']['status'], 'queued')
                self.assertTrue(all(job.status is JobStatus.QUEUED for job in app.metadata.list_jobs()))
                self.assertIsNone(app.metadata.list_documents()[0].active_version_id)

    def test_wizard_import_and_status_share_the_same_application(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source = root / 'owned.txt'; source.write_text('Owned source.')
            with make_app(root) as app, redirect_stdout(io.StringIO()) as output, \
                    patch('builtins.input', side_effect=['3', str(source), '', '4', '0']):
                wizard(app, status=_status, job_status=_job_status)
                self.assertEqual(len(app.metadata.list_documents()), 1)
                self.assertTrue(app.metadata.list_documents()[0].active_version_id)
                self.assertIn('"status": "succeeded"', output.getvalue())
