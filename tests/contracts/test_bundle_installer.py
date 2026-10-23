"""An operator can verify/install a pinned bundle without the build tool's dependencies."""
import json
from pathlib import Path
import subprocess
import sys
import unittest

from tests.contracts import test_release_bundle as fixtures
from tools.bundle_installer import digest, verify_approved, install_verified
from tools.release_bundle import freeze, verify


class BundleInstallerTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.ReleaseBundleTests()
        self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.bundle=self.fixture.bundle
        self.wheel=self.fixture.project()
        self.manifest=freeze(self.bundle)
        self.approved=digest(self.bundle/'manifest.json')

    def command(self,*args):
        # -S removes site-packages, including packaging, even on the development venv.
        return [sys.executable,'-I','-S','-B',str(self.bundle/'install_bundle.py'),
                str(self.bundle),'--manifest-sha256',self.approved.upper(),*map(str,args)]

    def test_standalone_verify_without_site_packages_and_legacy_bundle_support(self):
        result=subprocess.run(self.command('--verify-only'),cwd=self.bundle,capture_output=True,text=True,check=True)
        self.assertTrue(json.loads(result.stdout)['verified'])
        self.assertEqual(verify(self.bundle),self.manifest)
        # Existing schema1 bundles remain usable with the separately trusted new tool.
        manifest={key:value for key,value in self.manifest.items() if key!='installer'}
        (self.bundle/'manifest.json').write_text(json.dumps(manifest))
        self.assertEqual(verify_approved(self.bundle,digest(self.bundle/'manifest.json')),manifest)

    def test_standalone_install_uses_fresh_offline_environment_and_retains_receipt(self):
        environment=self.bundle/'sibling-environment'
        # The candidate bundle is a sibling, not the installation's parent.
        candidate=self.bundle/'candidate';candidate.mkdir()
        (candidate/'wheelhouse').mkdir()
        import shutil
        shutil.copy2(self.wheel,candidate/'wheelhouse'/self.wheel.name)
        freeze(candidate)
        command=[sys.executable,'-I','-S','-B',str(candidate/'install_bundle.py'),str(candidate),
            '--manifest-sha256',digest(candidate/'manifest.json'),'--environment',str(environment)]
        result=subprocess.run(command,cwd=self.bundle,capture_output=True,text=True,check=True)
        receipt=json.loads(result.stdout)
        self.assertTrue(receipt['offline_install'] and receipt['pip_check'])
        self.assertEqual(json.loads((environment/'bundle-install.json').read_text()),receipt)
        python=environment/('Scripts/python.exe' if sys.platform=='win32' else 'bin/python')
        if sys.platform!='win32':self.assertTrue(python.is_symlink())
        actual=subprocess.run([str(python),'-I','-B','-c',
            f"import sys,importlib.metadata;assert sys.prefix=={str(environment)!r};print(importlib.metadata.version('graphmind-rag-mcp'))"],
            capture_output=True,text=True,check=True)
        self.assertEqual(actual.stdout.strip(),'1.0')
        self.assertNotEqual(subprocess.run(command,capture_output=True).returncode,0)

    def test_tampered_manifest_wheel_lock_notice_or_installer_cannot_begin_install(self):
        for relative in ('manifest.json','wheelhouse/'+self.wheel.name,'requirements.lock',
                         next(iter(self.manifest['notices'])),'install_bundle.py'):
            with self.subTest(relative=relative):
                path=self.bundle/relative;original=path.read_bytes();path.write_bytes(original+b'changed')
                try:
                    with self.assertRaises(ValueError):verify_approved(self.bundle,self.approved)
                    if relative=='install_bundle.py':
                        with self.assertRaisesRegex(ValueError,'installer bytes'):verify(self.bundle)
                finally:path.write_bytes(original)

    def test_foreign_target_and_unsafe_approved_paths_fail_before_creating_environment(self):
        changes=[{'target':{**self.manifest['target'],'platform':'foreign-target'}},
                 {'notices':{'notices/../../outside':'0'*64}},
                 {'wheels':[{**self.manifest['wheels'][0],'filename':'../../outside.whl'}]}]
        path=self.bundle/'manifest.json'
        for change in changes:
            with self.subTest(change=change):
                path.write_text(json.dumps({**self.manifest,**change}))
                with self.assertRaises(ValueError):verify_approved(self.bundle,digest(path))
        with self.assertRaises(ValueError):verify_approved(self.bundle,'not-a-release-hash')

    def test_revoked_distribution_fails_even_with_newly_approved_manifest_hash(self):
        revoked={**self.manifest['wheels'][0], 'name':'litellm', 'version':'1.107.0'}
        manifest={**self.manifest, 'wheels':[revoked, *self.manifest['wheels']]}
        path=self.bundle/'manifest.json'
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'Revoked compromised distribution'):
            verify_approved(self.bundle,digest(path))
        environment=self.bundle/'must-not-exist'
        with self.assertRaisesRegex(ValueError, 'Revoked compromised distribution'):
            install_verified(self.bundle,environment,manifest)
        self.assertFalse(environment.exists())
