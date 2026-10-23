"""Reviewed notices must follow the exact artifact and survive clean-host verification."""
import json
from pathlib import Path
import subprocess
import sys
import unittest

from tests.contracts import test_release_bundle as fixtures
from tools.bundle_installer import digest, verify_approved
from tools.release_bundle import freeze, verify, wheel_record


class SupplementalNoticeTests(unittest.TestCase):
    def setUp(self):
        fixture=fixtures.ReleaseBundleTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        self.bundle=fixture.bundle
        fixture.project(dependencies=('dependency==1.0',))
        self.wheel=fixture.wheel('dependency')
        self.source=self.bundle/'reviewed';self.source.mkdir()
        self.license=self.source/'LICENSE';self.license.write_bytes(b'reviewed upstream bytes\n')
        self.item={'distribution':'dependency','version':'1.0','wheel_sha256':digest(self.wheel),
            'file':'LICENSE','sha256':digest(self.license),'source_url':'https://example.org/pinned/LICENSE',
            'revision':'retained-commit'}
        self.inventory=self.source/'notices.json'

    def freeze(self):
        self.inventory.write_text(json.dumps({'schema_version':1,'notices':[self.item]}))
        return freeze(self.bundle,supplemental_notices=self.inventory)

    def test_exact_supplement_survives_standalone_verification_without_site_packages(self):
        original=digest(self.wheel);manifest=self.freeze()
        self.assertEqual(digest(self.wheel),original)
        self.assertEqual(wheel_record(self.wheel)['sha256'],original)
        item=manifest['supplemental_notices'][0]
        self.assertEqual((self.bundle/item['path']).read_bytes(),self.license.read_bytes())
        self.assertEqual(verify(self.bundle),manifest)
        result=subprocess.run([sys.executable,'-I','-S','-B',str(self.bundle/'install_bundle.py'),
            str(self.bundle),'--manifest-sha256',digest(self.bundle/'manifest.json'),'--verify-only'],
            check=True,capture_output=True,text=True)
        self.assertTrue(json.loads(result.stdout)['verified'])
        self.assertEqual(manifest['rights_disposition'],'requires review')

    def test_wrong_artifact_missing_source_or_changed_notice_cannot_freeze(self):
        changes=({'version':'2.0'},{'wheel_sha256':'0'*64},{'sha256':'0'*64},
                 {'file':'../outside'},{'source_url':'file:///private'},{'revision':''})
        original=dict(self.item)
        for change in changes:
            with self.subTest(change=change):
                self.item={**original,**change}
                with self.assertRaises(ValueError):self.freeze()
                self.assertFalse((self.bundle/'manifest.json').exists())

    def test_provenance_rebinding_and_notice_tampering_fail_both_verifiers(self):
        manifest=self.freeze();path=self.bundle/'manifest.json'
        for change in ({'version':'2.0'},{'wheel_sha256':'0'*64},{'path':'notices/other/LICENSE'}):
            with self.subTest(change=change):
                altered={**manifest,'supplemental_notices':[{**manifest['supplemental_notices'][0],**change}]}
                path.write_text(json.dumps(altered))
                with self.assertRaises(ValueError):verify(self.bundle)
                with self.assertRaises(ValueError):verify_approved(self.bundle,digest(path))
        path.write_text(json.dumps(manifest))
        (self.bundle/manifest['supplemental_notices'][0]['path']).write_bytes(b'changed')
        with self.assertRaises(ValueError):verify(self.bundle)
        with self.assertRaises(ValueError):verify_approved(self.bundle,digest(path))
