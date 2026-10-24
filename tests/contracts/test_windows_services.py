"""Windows preparation remains reviewable, scoped, offline, and independent of SCM."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tests.support.paths import REPOSITORY
from tools.windows_service_files import prepare, render, validate_spec
from tools.windows_installation import reviewed, service_command
from tools.windows_service_host import child_environment, clean_child_stop, main


class WindowsServicesTests(unittest.TestCase):
    def layout(self):
        return json.loads((REPOSITORY/'deploy/native/windows.example.json').read_text())

    def test_seven_roles_share_existing_cli_local_offline_settings_and_identity_boundaries(self):
        specs = render(self.layout())
        self.assertEqual(len(specs), 7)
        reader, worker = [specs['graphmind-example-' + role] for role in ('reader', 'worker')]
        self.assertIn('graphmind', reader['command'])
        self.assertEqual(reader['environment'], worker['environment'])
        self.assertEqual(reader['environment']['GRAPHMIND_CHROMA_MODE'], 'http')
        self.assertEqual(reader['environment']['HF_HUB_OFFLINE'], '1')
        self.assertIn('graphmind-example-keycloak', reader['dependencies'])
        self.assertIn('graphmind-example-postgresql', specs['graphmind-example-keycloak']['dependencies'])
        self.assertIn('--ssl-keyfile', reader['command'])
        self.assertNotIn('--ssl-keyfile', worker['command'])
        self.assertEqual([s for s in specs.values() if s['environment_file']],
                         [specs['graphmind-example-keycloak']])

    def test_rejects_overlapping_state_partial_tls_unscoped_names_and_shell_launchers(self):
        for update in ({'name': 'spooler'}, {'data_dir': 'C:\\'}, {'reader_port': 443},
                       {'reader_port': 21801}, {'ollama_home': r'C:\GraphMind\state\application\nested'},
                       {'model_dir': r'C:\GraphMind\releases\candidate\models'},
                       {'config': r'\\server\share\config'}, {'python': 'C:\bad\npython.exe'}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                render({**self.layout(), **update})
        layout = self.layout(); del layout['ssl_keyfile']
        with self.assertRaises(ValueError):
            render(layout)
        layout = self.layout(); layout['backends']['keycloak']['command'] = [r'C:\Windows\cmd.exe', '/c', 'anything']
        with self.assertRaises(ValueError):
            render(layout)

    def test_review_requires_exact_manifest_and_all_seven_unchanged_spec_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)/'new'
            manifest = prepare(self.layout(), output)
            approved = hashlib.sha256((output/'installation.json').read_bytes()).hexdigest()
            self.assertEqual(reviewed(output, approved), manifest)
            with self.assertRaises(ValueError):
                reviewed(output, '0' * 64)
            with self.assertRaises(FileExistsError):
                prepare(self.layout(), output)
            target = output/'graphmind-example-reader.json'
            target.write_bytes(target.read_bytes() + b' ')
            with self.assertRaises(ValueError):
                reviewed(output, approved)

    def test_child_environment_does_not_inherit_operator_credentials_or_admin_authority(self):
        spec = render(self.layout())['graphmind-example-reader']
        env = child_environment(spec, {'SystemRoot': r'C:\Windows', 'AWS_SECRET_ACCESS_KEY': 'not-inherited',
                                      'PYTHONPATH': 'not-inherited', 'GRAPHMIND_IDENTITY_ADMIN_CLIENT_SECRET': 'not-inherited'})
        self.assertNotIn('AWS_SECRET_ACCESS_KEY', env)
        self.assertNotIn('PYTHONPATH', env)
        self.assertNotIn('GRAPHMIND_IDENTITY_ADMIN_CLIENT_SECRET', env)
        self.assertEqual(env['HF_HUB_OFFLINE'], '1')
        with tempfile.TemporaryDirectory() as temp:
            secret_file = Path(temp)/'env.json'
            secret_file.write_text(json.dumps({'GRAPHMIND_IDENTITY_ADMIN_CLIENT_SECRET': 'reject'}))
            spec['environment_file'] = str(secret_file)
            with self.assertRaises(ValueError):
                child_environment(spec, {})

    def test_requested_java_interrupt_exit_is_distinct_from_crash_and_other_failures(self):
        specs = render(self.layout())
        for role in ('keycloak', 'neo4j'):
            spec = specs['graphmind-example-'+role]
            self.assertTrue(clean_child_stop(spec, 130, stop_requested=True, ctrl_c_sent=True))
            self.assertTrue(clean_child_stop(spec, 0, stop_requested=True, ctrl_c_sent=False))
            self.assertFalse(clean_child_stop(spec, 130, stop_requested=False, ctrl_c_sent=True))
            self.assertFalse(clean_child_stop(spec, 130, stop_requested=True, ctrl_c_sent=False))
            for code in (1, 2, 143, 0xC000013A, -1073741510):
                with self.subTest(role=role, code=code):
                    self.assertFalse(clean_child_stop(spec, code, stop_requested=True, ctrl_c_sent=True))
            wrong_executable = {**spec, 'command': [r'C:\\native\\python.exe']}
            self.assertFalse(clean_child_stop(wrong_executable, 130, stop_requested=True, ctrl_c_sent=True))
        for role in ('reader', 'worker', 'chroma', 'postgresql', 'ollama'):
            spec = specs['graphmind-example-'+role]
            self.assertFalse(clean_child_stop(spec, 130, stop_requested=True, ctrl_c_sent=True))
            self.assertFalse(clean_child_stop(spec, 0, stop_requested=False, ctrl_c_sent=True))

    def test_restored_reader_and_worker_fail_before_dispatch_while_sealed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root/'RESTORE_SEALED.json').write_text('{}')
            for role in ('reader', 'worker'):
                target = root/'spec.json'
                target.write_text(json.dumps(render(self.layout())['graphmind-example-' + role]))
                with patch.object(sys, 'platform', 'win32'), patch.object(sys, 'argv', ['host', str(target)]), \
                        patch('tools.windows_service_host.run_service') as dispatch:
                    with self.assertRaises(SystemExit):
                        main()
                    dispatch.assert_not_called()

    def test_scm_binary_path_quotes_native_paths_and_uses_isolated_maintenance_python(self):
        args = [r'C:\Graph Mind\maintenance\python.exe', '-I', '-B',
                r'C:\Graph Mind\maintenance-tools\windows_service_host.py', r'C:\Graph Mind\services\reader.json']
        self.assertEqual(service_command(args[0], args[3], args[4]), subprocess.list2cmdline(args))

    def test_installed_sibling_scripts_import_with_isolated_python(self):
        # Neither the SCM host nor installed operator CLI may require the checkout.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            assets = root/'operator-assets'; assets.mkdir()
            elsewhere = root/'unrelated-cwd'; elsewhere.mkdir()
            for name in ('windows_service_files.py', 'windows_service_host.py', 'windows_installation.py'):
                (assets/name).write_bytes((REPOSITORY/'tools'/name).read_bytes())
            result = subprocess.run([sys.executable, '-I', '-B', str(assets/'windows_service_host.py')],
                                    cwd=elsewhere, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('one protected specification', result.stderr)
            self.assertNotIn('ModuleNotFoundError', result.stderr)
            installer = subprocess.run([sys.executable, '-I', '-B', str(assets/'windows_installation.py'), '--help'],
                                      cwd=elsewhere, capture_output=True, text=True)
            self.assertEqual(installer.returncode, 0, installer.stderr)
            self.assertIn('--manifest-sha256', installer.stdout)
            self.assertNotIn('ModuleNotFoundError', installer.stderr)


if __name__ == '__main__':
    unittest.main()
