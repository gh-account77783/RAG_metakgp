"""Low-space admission preserves existing sources and permits cleanup/retry."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from graphmind.config import Settings
from graphmind.errors import ConfigurationError, StorageCapacityError
from graphmind.storage_budget import storage_readiness
from tests.support.application import import_and_run, make_app


class StorageBudgetTests(unittest.TestCase):
    def test_low_space_refuses_import_before_extraction_and_staging(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source = root / 'owned.txt'; source.write_text('Owned facts.')
            with make_app(root) as app, patch('graphmind.storage_budget.disk_capacity',
                    return_value={"free_bytes": 5, "reserve_bytes": 5}), \
                    patch.object(app.ingestion.extractor, 'extract') as extraction:
                with self.assertRaises(StorageCapacityError):
                    app.ingestion.prepare_import(source)
                extraction.assert_not_called()
                self.assertEqual(app.metadata.list_jobs(), [])
                self.assertEqual(app.metadata.list_documents(), [])
                self.assertEqual(list(app.settings.files_dir.iterdir()), [])

    def test_queued_replacement_retains_old_head_then_retries_after_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source = root / 'owned.txt'; source.write_text('The code is CERULEAN.')
            with make_app(root) as app:
                original = import_and_run(app, source)
                source.write_text('The code is AMBER.')
                replacement = app.ingestion.prepare_import(source)
                with patch('graphmind.storage_budget.disk_capacity', return_value={"free_bytes": 0, "reserve_bytes": 1}):
                    with self.assertRaises(StorageCapacityError):
                        app.jobs.run_job(replacement.job.job_id)
                    self.assertFalse(storage_readiness(app.settings).available)
                    self.assertEqual(app.metadata.job(replacement.job.job_id).error_code, 'storage_capacity_unavailable')
                    self.assertEqual(app.metadata.document(original.document.document_id).active_version_id,
                                     original.version.version_id)
                    self.assertIn('CERULEAN', app.answers.query('What is the code?').answer)
                app.jobs.run_job(replacement.job.job_id)
                self.assertTrue(storage_readiness(app.settings).available)
                self.assertEqual(app.metadata.document(original.document.document_id).active_version_id,
                                 replacement.version.version_id)
                self.assertIn('AMBER', app.answers.query('What is the code?').answer)
                with patch('graphmind.storage_budget.disk_capacity', return_value={"free_bytes": 0, "reserve_bytes": 1}):
                    deletion = app.ingestion.prepare_delete(original.document.document_id)
                    app.jobs.run_job(deletion.job.job_id)
                    self.assertIsNotNone(app.metadata.document(original.document.document_id).deleted_at)
                    self.assertEqual(list(app.settings.files_dir.iterdir()), [])

    def test_capacity_probe_failure_is_typed_and_reserve_is_configurable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = Settings.load(dotenv_path=root/'missing.env',
                                     environ={'GRAPHMIND_MIN_FREE_DISK_BYTES': '1024'})
            self.assertEqual(settings.min_free_disk_bytes, 1024)
            with make_app(root) as app, patch('graphmind.storage_budget.disk_capacity', side_effect=OSError):
                self.assertFalse(storage_readiness(app.settings).available)
            with self.assertRaises(ConfigurationError):
                Settings.load(dotenv_path=root/'missing.env', environ={},
                              explicit={'min_free_disk_bytes': 0})
