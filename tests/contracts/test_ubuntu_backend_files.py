"""Native backend files share isolation defaults and never activate a service."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tools.ubuntu_backend_files import render
from tests.support.paths import REPOSITORY


class UbuntuBackendFilesTests(unittest.TestCase):
    def layout(self):
        return json.loads((REPOSITORY/'deploy/native/ubuntu.backends.example.json').read_text())

    def test_roles_keep_separate_state_and_vendor_commands(self):
        layout = self.layout(); before = copy.deepcopy(layout)
        units = render(layout)
        self.assertEqual(layout, before)
        self.assertEqual(len(units), 3)
        pg = units['graphmind-example-postgresql.service']
        kc = units['graphmind-example-keycloak.service']
        neo = units['graphmind-example-neo4j.service']
        self.assertIn('ExecStart=:' + json.dumps(layout['postgresql']['binary']) +
                      ' "-D" ' + json.dumps(layout['postgresql']['data_dir']), pg)
        self.assertIn('KillSignal=SIGINT', pg)
        self.assertIn('Requires=graphmind-example-postgresql.service', kc)
        self.assertIn('EnvironmentFile=/etc/graphmind/keycloak.env', kc)
        self.assertIn('"start" "--optimized"', kc)
        self.assertIn('"console"', neo)
        self.assertIn('LimitNOFILE=60000', neo)
        for role, spec in ((r, layout[r]) for r in ('postgresql', 'keycloak', 'neo4j')):
            unit = units['graphmind-example-' + role + '.service']
            self.assertIn('User=' + spec['user'], unit)
            self.assertIn('ReadWritePaths=' + json.dumps(spec['state_dir']), unit)
            for property in ('ProtectHome=true', 'ProtectSystem=strict', 'NoNewPrivileges=true',
                             'CapabilityBoundingSet=', 'KillMode=mixed', 'UMask=0077'):
                self.assertIn(property, unit)

    def test_scalar_paths_keep_literal_spaces_and_escape_specifiers(self):
        layout = self.layout()
        layout['keycloak']['environment_file'] = '/etc/Graph Mind %/keycloak.env'
        layout['keycloak']['state_dir'] = '/var/lib/Graph Mind % keycloak'
        kc = render(layout)['graphmind-example-keycloak.service']
        self.assertIn('EnvironmentFile=/etc/Graph Mind %%/keycloak.env\n', kc)
        self.assertIn('WorkingDirectory=/var/lib/Graph Mind %% keycloak\n', kc)

    def test_invalid_accounts_paths_and_overlapping_state_fail(self):
        for role, changes in (
            ('postgresql', {'user': 'root'}),
            ('keycloak', {'user': 'graphmind-pg'}),
            ('neo4j', {'state_dir': '/var/lib/graphmind-postgresql/child'}),
            ('keycloak', {'environment_file': '/etc/config\nExecStart=/bad'}),
            ('neo4j', {'home': '/home/operator/neo4j'}),
            ('postgresql', {'data_dir': '/var/lib/unrelated'}),
            ('neo4j', {'state_dir': '/var/lib'}),
        ):
            layout = self.layout(); layout[role].update(changes)
            with self.subTest(role=role, changes=changes), self.assertRaises(ValueError):
                render(layout)

    def test_cli_retains_hashes_and_refuses_existing_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)/'backend files'
            command = [sys.executable, '-B', '-m', 'tools.ubuntu_backend_files',
                str(REPOSITORY/'deploy/native/ubuntu.backends.example.json'), str(output)]
            result = subprocess.run(command, cwd=REPOSITORY, capture_output=True, text=True, check=True)
            self.assertFalse(json.loads(result.stdout)['activated'])
            manifest = json.loads((output/'service-files.json').read_text())
            self.assertEqual(len(manifest['unit_sha256']), 3)
            for name, digest in manifest['unit_sha256'].items():
                self.assertEqual(hashlib.sha256((output/name).read_bytes()).hexdigest(), digest)
            before = (output/'service-files.json').read_bytes()
            self.assertNotEqual(subprocess.run(command, cwd=REPOSITORY, capture_output=True).returncode, 0)
            self.assertEqual((output/'service-files.json').read_bytes(), before)
