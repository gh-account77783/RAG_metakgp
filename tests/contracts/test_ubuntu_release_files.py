"""Immutable release overrides switch the Python closure without changing shared state."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests.support.paths import REPOSITORY
from tools.ubuntu_release_files import prepare, release_overrides
from tools.ubuntu_service_files import render


class UbuntuReleaseFilesTests(unittest.TestCase):
    def layout(self):
        return json.loads((REPOSITORY/'deploy/native/ubuntu.example.json').read_text())

    def test_three_commands_switch_together_with_literal_release_paths(self):
        layout = self.layout()
        before = dict(layout)
        release = '/opt/Graph Mind %$MODEL "release"/r2'
        files, manifest = release_overrides(layout, release)
        original = render(layout)
        candidate = render({**layout,'python':release+'/bin/python','chroma':release+'/bin/chroma'})
        self.assertEqual(layout,before)
        self.assertEqual(len(files),3)
        for role in ('reader','worker','chroma'):
            unit = layout['name']+'-'+role+'.service'
            body = files[unit+'.d/50-graphmind-release.conf']
            self.assertEqual(body.splitlines()[:2],['[Service]','ExecStart='])
            command, = [line for line in candidate[unit].splitlines() if line.startswith('ExecStart=')]
            self.assertEqual(body.splitlines()[2],command)
            # Command is the only changed setting; retain service account, env, state and limits.
            expected = [line for line in original[unit].splitlines() if not line.startswith('ExecStart=')]
            self.assertEqual([line for line in candidate[unit].splitlines() if not line.startswith('ExecStart=')],expected)
        ollama = layout['name']+'-ollama.service'
        self.assertEqual(candidate[ollama],original[ollama])
        self.assertFalse(manifest['activated'])
        self.assertIn('%%$MODEL',next(iter(files.values())))

    def test_mixed_environments_and_state_overlap_fail_before_output(self):
        layout = self.layout()
        old = str(Path(layout['python']).parent.parent).replace('\\','/')
        for release in ('relative','/','/home/operator/r2',old,old+'/nested',
                        layout['data_dir'],layout['model_dir']+'/r2','/opt/a\nExecStart=/bad'):
            with self.subTest(release=release), self.assertRaises(ValueError):
                release_overrides(layout,release)
        with self.assertRaises(ValueError):
            release_overrides({**layout,'chroma':'/opt/unrelated/bin/chroma'},'/opt/releases/r2')
        with tempfile.TemporaryDirectory() as temporary:
            output=Path(temporary)/'output'
            with self.assertRaises(ValueError): prepare(layout,layout['data_dir'],output)
            self.assertFalse(output.exists())

    def test_standalone_cli_retains_hashes_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            output=Path(temporary)/'files'
            command=[sys.executable,'-B',str(REPOSITORY/'tools/ubuntu_release_files.py'),
                str(REPOSITORY/'deploy/native/ubuntu.example.json'),'/opt/graphmind/releases/r2',str(output)]
            result=subprocess.run(command,capture_output=True,text=True,check=True)
            self.assertFalse(json.loads(result.stdout)['activated'])
            original=(output/'release-files.json').read_bytes()
            manifest=json.loads(original)
            for name,digest in manifest['file_sha256'].items():
                self.assertEqual(hashlib.sha256((output/name).read_bytes()).hexdigest(),digest)
            self.assertNotEqual(subprocess.run(command,capture_output=True).returncode,0)
            self.assertEqual((output/'release-files.json').read_bytes(),original)
