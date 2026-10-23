"""Generated service artifacts refuse unsafe layouts and preserve literal paths."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tools.ubuntu_service_files import render
from tests.support.paths import REPOSITORY


class UbuntuServiceFilesTests(unittest.TestCase):
    def layout(self):
        return json.loads((REPOSITORY/'deploy/native/ubuntu.example.json').read_text())

    def test_optional_tls_pair_is_only_passed_to_reader_and_survives_release_update(self):
        layout={**self.layout(),'ssl_certfile':'/etc/graphmind/tls/chain.pem','ssl_keyfile':'/etc/graphmind/tls/key.pem'}
        units=render(layout)
        for name,body in units.items():
            self.assertEqual('--ssl-keyfile' in body,name.endswith('-reader.service'))
        from tools.ubuntu_release_files import release_overrides
        changes,_=release_overrides(layout,'/opt/graphmind/releases/new')
        self.assertIn('--ssl-keyfile',changes[layout['name']+'-reader.service.d/50-graphmind-release.conf'])
        for field in ('ssl_certfile','ssl_keyfile'):
            with self.assertRaises(ValueError):render({**self.layout(),field:layout[field]})

    def test_unsafe_layouts_fail_before_generating_units(self):
        for overrides in ({'user': 'root'}, {'name': 'graphmind-test\nUser=root'},
                          {'reader_port': 443}, {'reader_port': 19801},
                          {'model_dir': '/var/lib/graphmind/data/models'},
                          {'config': '/home/operator/config.toml'}, {'python': '/opt/a\nExecStart=/bad'}):
            layout = {**self.layout(), **overrides}
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                render(layout)

    def test_literal_paths_cannot_expand_environment_or_systemd_specifiers(self):
        layout = self.layout(); original = copy.deepcopy(layout)
        layout['python'] = '/opt/Graph Mind %$USER "owned"/bin/python'
        units = render(layout)
        reader = units[layout['name']+'-reader.service']
        self.assertIn('ExecStart=:' + json.dumps(layout['python'].replace('%', '%%')), reader)
        self.assertEqual(len([line for line in reader.splitlines() if line.startswith('ExecStart=')]), 1)
        self.assertIn('GRAPHMIND_CHROMA_MODE=http', reader)
        self.assertIn('KillMode=mixed', reader)
        self.assertEqual(original['name'], layout['name'])

    def test_cli_retains_unit_hashes_and_refuses_existing_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)/'service files'
            command = [sys.executable, '-B', str(REPOSITORY/'tools/ubuntu_service_files.py'),
                       str(REPOSITORY/'deploy/native/ubuntu.example.json'), str(output)]
            result = subprocess.run(command, capture_output=True, text=True, check=True)
            self.assertFalse(json.loads(result.stdout)['activated'])
            manifest = json.loads((output/'service-files.json').read_text())
            self.assertEqual(len(manifest['unit_sha256']), 4)
            for name, digest in manifest['unit_sha256'].items():
                self.assertEqual(hashlib.sha256((output/name).read_bytes()).hexdigest(), digest)
            before = (output/'service-files.json').read_bytes()
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertEqual((output/'service-files.json').read_bytes(), before)
