"""Whole-stack installation refuses cross-layout collisions and unreviewed units."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.support.paths import REPOSITORY
from tools.ubuntu_installation import complete_layout, install_new, prepare, preflight, reviewed


class UbuntuInstallationTests(unittest.TestCase):
    def setUp(self):
        self.app=json.loads((REPOSITORY/'deploy/native/ubuntu.example.json').read_text())
        self.backends=json.loads((REPOSITORY/'deploy/native/ubuntu.backends.example.json').read_text())
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.directory=Path(tmp.name)/'prepared'

    def test_complete_layout_reuses_renderers_and_pins_backend_order(self):
        manifest=prepare(self.app,self.backends,self.directory)
        self.assertEqual(len(manifest['unit_sha256']),7)
        reader=(self.directory/(self.app['name']+'-reader.service')).read_text()
        self.assertIn('After=graphmind-example-postgresql.service graphmind-example-keycloak.service graphmind-example-neo4j.service',reader)
        approved=hashlib.sha256((self.directory/'installation.json').read_bytes()).hexdigest()
        self.assertEqual(reviewed(self.directory,approved),manifest)
        with self.assertRaises(FileExistsError):prepare(self.app,self.backends,self.directory)
        unit=self.directory/next(iter(manifest['unit_sha256']));unit.write_text('changed')
        with self.assertRaisesRegex(ValueError,'bytes changed'):reviewed(self.directory,approved)

    def test_cross_layout_state_account_namespace_and_release_collisions_are_rejected(self):
        for role,field,value in [('postgresql','state_dir',self.app['data_dir']),
                                 ('keycloak','user',self.app['user'])]:
            backend=copy.deepcopy(self.backends);backend[role][field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):complete_layout(self.app,backend)
        with self.assertRaises(ValueError):complete_layout({**self.app,'name':'graphmind-other'},self.backends)
        with self.assertRaises(ValueError):complete_layout({**self.app,'chroma':'/opt/other/bin/chroma'},self.backends)

    def test_install_refuses_existing_namespace_before_any_system_changes(self):
        manifest=prepare(self.app,self.backends,self.directory)
        target=self.directory/'systemd';target.mkdir()
        name=next(iter(manifest['unit_sha256']));(target/name).write_bytes(b'existing service')
        with patch('tools.ubuntu_installation.subprocess.run') as run:
            with self.assertRaisesRegex(ValueError,'existing service'):install_new(self.directory,manifest,target)
        run.assert_not_called()
        self.assertEqual((target/name).read_bytes(),b'existing service')

    def test_install_writes_only_reviewed_seven_units_and_never_activates_services(self):
        manifest=prepare(self.app,self.backends,self.directory)
        target=self.directory/'systemd';target.mkdir()
        from types import SimpleNamespace
        def command(args,**kwargs):
            return SimpleNamespace(stdout='not-found\n',returncode=0)
        with patch('tools.ubuntu_installation.subprocess.run',side_effect=command) as run:
            result=install_new(self.directory,manifest,target)
        self.assertEqual({p.name for p in target.iterdir()},set(manifest['unit_sha256']))
        for name,expected in manifest['unit_sha256'].items():
            self.assertEqual(hashlib.sha256((target/name).read_bytes()).hexdigest(),expected)
        self.assertFalse(result['services_activated'])
        self.assertTrue((self.directory/'installed.json').is_file())
        self.assertFalse(any(c.args[0][1] in {'start','enable','restart'} for c in run.call_args_list))

    def test_preflight_records_effective_access_and_disk_failures_without_reading_secrets(self):
        manifest=prepare(self.app,self.backends,self.directory)
        # Failure contract only; live Unix account/access checks are qualified on Ubuntu.
        from types import SimpleNamespace
        accounts={name:SimpleNamespace(pw_uid=i+100) for i,name in enumerate(manifest['accounts'])}
        with patch.dict('sys.modules',{'pwd':SimpleNamespace(getpwnam=accounts.__getitem__)}),patch(
                'tools.ubuntu_installation.subprocess.run',return_value=SimpleNamespace(returncode=1)),patch(
                'tools.ubuntu_installation.shutil.disk_usage',return_value=SimpleNamespace(free=1)):
            result=preflight(manifest)
        self.assertFalse(result['preflight_passed'])
        self.assertIn({'reason':'less_than_512_MiB_free'},result['failures'])
        self.assertFalse(result['configuration_values_checked'])
