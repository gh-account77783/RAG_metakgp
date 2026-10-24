"""Owned filesystem proofs for sealed recovery; SCM/ACLs still need native acceptance."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools import windows_operations as operations
from tools.windows_service_files import ROLES
from tests.support.paths import REPOSITORY


def service_specs():
    result = {}
    for role in ROLES:
        executable = 'chroma.exe' if role == 'chroma' else 'python.exe'
        result[role] = dict(schema_version=1, name='graphmind-test-'+role, role=role,
            command=[str(Path(r'C:\Software\releases\r1\Scripts')/executable)], environment={},
            environment_file=None, cwd='C:\\State\\'+role, state_dir='C:\\State\\'+role,
            dependencies=[], stop_command=None, stop_timeout_seconds=10, memory_bytes=256*1024**2)
    result['ollama']['environment']['OLLAMA_MODELS'] = r'C:\Models'
    return result


class StoppedSCM:
    def __init__(self):
        self.states = {}; self.failed = set(); self.stops = []; self.start_types = {}

    def state(self, name):
        return self.states.get(name, 1)

    def clean_stopped(self, name):
        return self.state(name) == 1 and name not in self.failed

    def stop(self, name):
        self.stops.append(name); self.states[name] = 1

    def start_type(self, name):
        return self.start_types.get(name, 2)

    def disable(self, name):
        self.start_types[name] = 4


class WindowsOperationsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.specs = service_specs()
        self.scm = StoppedSCM()

    def native_file_boundary(self):
        """Model NTFS ACL/copy adapters, while all state hashes/moves use real files."""
        self.acls = {}
        def get_acl(path):
            return self.acls.get(str(Path(path)), 'original-owner-group-dacl-sacl')
        def set_acl(path, value):
            self.acls[str(Path(path))] = value
        def copy(source, target):
            shutil.copytree(source, target, dirs_exist_ok=True)
        for name, replacement in [('validate_spec', lambda spec: spec), ('get_acl', get_acl),
                ('set_acl', set_acl), ('copy_tree', copy), ('private_directory', lambda path: Path(path).mkdir())]:
            handle = patch.object(operations, name, replacement); handle.start(); self.addCleanup(handle.stop)
        handle = patch.object(operations, 'reject_alternate_streams'); handle.start(); self.addCleanup(handle.stop)
        handle = patch.object(operations.shutil, 'disk_usage', return_value=SimpleNamespace(free=10**12))
        handle.start(); self.addCleanup(handle.stop)
        self.roots = {}
        for label in ('application', 'configuration', 'postgresql', 'keycloak', 'neo4j', 'ollama_home'):
            directory = self.base/label; directory.mkdir()
            (directory/'state.bin').write_bytes((label+'-before').encode())
            self.roots[label] = str(directory)
        for role in ROLES:
            label = role if role in ('postgresql', 'keycloak', 'neo4j') else 'ollama_home' if role == 'ollama' else 'application'
            self.specs[role].update(state_dir=self.roots[label], cwd=self.roots[label])
        self.specs['reader']['command'] += ['--config', str(self.base/'configuration/config.toml'),
                                           '--dotenv', str(self.base/'configuration/reader.env')]

    def test_stop_orders_readers_before_backends_and_refuses_dirty_stop(self):
        self.scm.states = {spec['name']: 4 for spec in self.specs.values()}
        operations.quiesce(self.specs, self.scm)
        self.assertEqual(self.scm.stops, ['graphmind-test-'+role for role in operations.STOP_ORDER])
        self.scm.failed.add('graphmind-test-postgresql')
        with self.assertRaisesRegex(ValueError, 'successful exit'):
            operations.require_stopped(self.specs, self.scm)
        self.scm.states['graphmind-test-reader'] = 2
        with self.assertRaisesRegex(ValueError, 'stoppable'):
            operations.quiesce(self.specs, self.scm)

    def test_optional_public_proxy_is_disabled_stopped_first_and_never_reenabled(self):
        proxy = 'graphmind-test-proxy'
        self.scm.states = {spec['name']: 4 for spec in self.specs.values()}
        self.scm.states[proxy] = 4
        result = operations.quiesce(self.specs, self.scm, proxy)
        self.assertEqual(self.scm.stops[0], proxy)
        self.assertEqual(self.scm.start_type(proxy), 4)
        self.assertEqual(result['proxy_previous_start_type'], 2)
        operations.require_stopped(self.specs, self.scm, proxy)
        self.scm.start_types[proxy] = 2
        with self.assertRaisesRegex(ValueError, 'DISABLED'):
            operations.require_stopped(self.specs, self.scm, proxy)
        with self.assertRaisesRegex(ValueError, 'namespace-proxy'):
            operations.quiesce(self.specs, self.scm, 'unrelated-proxy')

    def test_snapshot_restore_keeps_latest_tree_and_never_restarts_reader(self):
        self.native_file_boundary()
        snapshot = self.base/'snapshot'
        receipt = operations.backup(self.specs, self.roots, snapshot, self.scm)
        current = Path(self.roots['application'])/'state.bin'
        current.write_bytes(b'after-backup-tombstone')
        seal = self.base/'RESTORE_SEALED.json'
        result = operations.restore(self.specs, snapshot, receipt['snapshot_sha256'], seal, self.scm)
        self.assertEqual(current.read_bytes(), b'application-before')
        self.assertEqual((self.base/'application.pre-restore/state.bin').read_bytes(), b'after-backup-tombstone')
        self.assertTrue(json.loads(seal.read_text())['latest_account_and_tombstone_reconciliation_required'])
        self.assertTrue(result['sealed']); self.assertFalse(result['services_started'])
        self.assertFalse(self.scm.stops)
        with self.assertRaises(FileExistsError):
            operations.restore(self.specs, snapshot, receipt['snapshot_sha256'], seal, self.scm)

    def test_backup_rejects_live_dirty_missing_scope_and_low_disk_before_copy(self):
        self.native_file_boundary()
        snapshot = self.base/'snapshot'
        self.scm.states['graphmind-test-reader'] = 4
        with self.assertRaises(ValueError): operations.backup(self.specs, self.roots, snapshot, self.scm)
        self.scm.states.clear(); self.scm.failed.add('graphmind-test-neo4j')
        with self.assertRaises(ValueError): operations.backup(self.specs, self.roots, snapshot, self.scm)
        self.scm.failed.clear()
        roots = {key: value for key, value in self.roots.items() if key != 'ollama_home'}
        with self.assertRaisesRegex(ValueError, 'every service state'):
            operations.backup(self.specs, roots, snapshot, self.scm)
        with patch.object(operations.shutil, 'disk_usage', return_value=SimpleNamespace(free=1)):
            with self.assertRaisesRegex(ValueError, 'capacity'):
                operations.backup(self.specs, self.roots, snapshot, self.scm)
        self.assertFalse(snapshot.exists())

    def test_tampered_snapshot_fails_before_replacement_or_sealing(self):
        self.native_file_boundary()
        snapshot = self.base/'snapshot'
        receipt = operations.backup(self.specs, self.roots, snapshot, self.scm)
        (snapshot/'application/state.bin').write_bytes(b'tampered')
        seal = self.base/'RESTORE_SEALED.json'
        with self.assertRaisesRegex(ValueError, 'content verification'):
            operations.restore(self.specs, snapshot, receipt['snapshot_sha256'], seal, self.scm)
        self.assertFalse(seal.exists())
        self.assertFalse((self.base/'application.pre-restore').exists())
        self.assertEqual((self.base/'application/state.bin').read_bytes(), b'application-before')

    def test_failed_scratch_acl_verification_retains_seal_and_current_state(self):
        self.native_file_boundary()
        snapshot = self.base/'snapshot'
        receipt = operations.backup(self.specs, self.roots, snapshot, self.scm)
        seal = self.base/'RESTORE_SEALED.json'
        with patch.object(operations, 'get_acl', side_effect=lambda p: 'wrong' if '.restore-stage' in str(p) else
                          'original-owner-group-dacl-sacl'):
            with self.assertRaisesRegex(ValueError, 'ACL verification'):
                operations.restore(self.specs, snapshot, receipt['snapshot_sha256'], seal, self.scm)
        self.assertTrue(seal.exists())
        self.assertTrue((self.base/'application.restore-stage').exists())
        self.assertFalse((self.base/'application.pre-restore').exists())

    def test_release_update_rollback_and_drift_keep_complete_original_specs(self):
        candidate = copy.deepcopy(self.specs)
        for role in operations.UPDATE_ROLES:
            candidate[role]['command'][0] = candidate[role]['command'][0].replace('r1', 'r2')
        output, live = self.base/'release-change', self.base/'live'
        live.mkdir()
        receipt = operations.prepare_update(self.specs, candidate, output)
        for filename in json.loads((output/'update.json').read_text())['before']:
            shutil.copyfile(output/'before'/filename, live/filename)
        with patch.object(operations, 'get_acl', return_value='protected-ACL'), patch.object(operations, 'set_acl'):
            result = operations.apply_update(self.specs, output, receipt['update_sha256'], live, self.scm)
            self.assertFalse(result['services_started'])
            operations.apply_update(candidate, output, receipt['update_sha256'], live, self.scm, rollback=True)
            for filename in json.loads((output/'update.json').read_text())['before']:
                self.assertEqual((live/filename).read_bytes(), (output/'before'/filename).read_bytes())
            (live/'graphmind-test-reader.json').write_text('operator drift')
            with self.assertRaisesRegex(ValueError, 'drift'):
                operations.apply_update(self.specs, output, receipt['update_sha256'], live, self.scm)
        self.assertFalse(any(live.glob('*.pending')))
        self.assertFalse(self.scm.stops)

    def test_update_rejects_nonrelease_changes_and_overlapping_closures(self):
        for change in ('configuration', 'backend', 'nested'):
            candidate = copy.deepcopy(self.specs)
            for role in operations.UPDATE_ROLES:
                candidate[role]['command'][0] = candidate[role]['command'][0].replace('r1', 'r2')
            if change == 'configuration': candidate['reader']['environment']['A'] = 'new'
            if change == 'backend': candidate['postgresql']['command'].append('unsafe-change')
            if change == 'nested':
                for role in operations.UPDATE_ROLES:
                    candidate[role]['command'][0] = candidate[role]['command'][0].replace('r2', r'r1\nested')
            with self.subTest(change=change), self.assertRaises(ValueError):
                operations.prepare_update(self.specs, candidate, self.base/change)
            self.assertFalse((self.base/change).exists())

    def test_native_copy_uses_no_purge_and_fails_on_robocopy_error(self):
        with patch.object(operations.subprocess, 'run', return_value=SimpleNamespace(returncode=8)) as run:
            with self.assertRaises(OSError): operations.copy_tree('source', 'target')
        args = run.call_args.args[0]
        self.assertIn('/COPY:DAT', args); self.assertIn('/R:0', args)
        self.assertNotIn('/MIR', args); self.assertNotIn('/PURGE', args)

    def test_installed_operations_help_uses_only_copied_siblings_outside_checkout(self):
        assets = self.base/'operator-assets'; assets.mkdir()
        elsewhere = self.base/'unrelated-cwd'; elsewhere.mkdir()
        for name in ('windows_operations.py', 'windows_installation.py', 'windows_service_files.py'):
            shutil.copyfile(REPOSITORY/'tools'/name, assets/name)
        result = subprocess.run([sys.executable, '-I', '-B', str(assets/'windows_operations.py'), '--help'],
            cwd=elsewhere, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('prepare-update', result.stdout)
        self.assertNotIn('ModuleNotFoundError', result.stderr)


if __name__ == '__main__':
    unittest.main()
