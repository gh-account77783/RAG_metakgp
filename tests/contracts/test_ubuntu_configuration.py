"""New native configuration separates authority and refuses destructive ambiguity."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from tests.support.paths import REPOSITORY
from tools.ubuntu_configuration import prepare, render


class UbuntuConfigurationTests(unittest.TestCase):
    def setUp(self):
        app=json.loads((REPOSITORY/'deploy/native/ubuntu.example.json').read_text())
        app.update(ssl_certfile='/etc/reader-tls/chain.pem',ssl_keyfile='/etc/reader-tls/key.pem')
        backend=json.loads((REPOSITORY/'deploy/native/ubuntu.backends.example.json').read_text())
        self.profile={'schema_version':1,'application':app,'backends':backend,
            'ports':dict(zip(('postgresql','keycloak_http','keycloak_https','keycloak_management','neo4j_bolt','neo4j_http'),range(25432,25438))),
            'import_dir':'/var/lib/graphmind-input','identity_ca_file':'/etc/graphmind-ca.pem',
            'keycloak_certfile':'/etc/keycloak-tls/chain.pem','keycloak_keyfile':'/etc/keycloak-tls/key.pem'}

    def test_reader_cannot_read_host_or_bootstrap_secrets_and_realm_keeps_pkce(self):
        files=render(self.profile,b'[graphmind]\nanswer_mode="local"\n')
        by_name={Path(f.target).name:f for f in files}
        reader=by_name['reader.env'];admin=by_name['host-admin.env'];kc=by_name['keycloak.env']
        self.assertNotIn(b'ADMIN_CLIENT_SECRET',reader.content)
        self.assertIn(b'ADMIN_CLIENT_SECRET',admin.content)
        self.assertNotIn(b'KC_BOOTSTRAP_ADMIN_PASSWORD',reader.content+admin.content)
        self.assertEqual((reader.mode,admin.mode,kc.mode),(0o640,0o600,0o600))
        self.assertNotIn(reader.content.decode(),repr(reader))
        realm=json.loads(by_name['realm-import.json'].content)
        self.assertFalse(any('${' in json.dumps(c) for c in realm['clients']))
        self.assertFalse(any(c.get('directAccessGrantsEnabled') for c in realm['clients']))
        client=next(c for c in realm['clients'] if c['clientId']=='graphmind-claude-code')
        self.assertEqual(client['attributes']['pkce.code.challenge.method'],'S256')
        self.assertEqual(len(realm['users']),1)
        self.assertEqual(realm['users'][0]['clientRoles']['realm-management'],['query-users','view-users','manage-users'])
        self.assertNotIn('credentials',realm['users'][0])
        self.assertIn(b'GRAPHMIND_BROWSER_COOKIE_SECURE="true"',reader.content)

    def test_port_import_and_tls_configuration_collisions_are_rejected(self):
        for edit in ('port','import','tls'):
            p=copy.deepcopy(self.profile)
            if edit=='port':p['ports']['postgresql']=p['application']['reader_port']
            if edit=='import':p['import_dir']=p['application']['data_dir']+'/input'
            if edit=='tls':p['keycloak_keyfile']='/etc/graphmind/old-secret.pem'
            with self.subTest(edit=edit),self.assertRaises(ValueError):render(p,b'')

    def test_credentials_relative_models_and_remote_profiles_are_rejected_before_writes(self):
        for settings in (b'neo4j_password="private"',b'visual_model_dir="../model"',b'answer_mode="cloud"'):
            with self.subTest(settings=settings),tempfile.TemporaryDirectory() as tmp:
                destination=Path(tmp)/'new'
                with self.assertRaises(ValueError):prepare(self.profile,settings,destination)
                self.assertFalse(destination.exists())

    def test_retry_preserves_generated_credentials_and_records_exact_bytes(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            destination=Path(tmp)/'new'
            result=prepare(self.profile,b'[graphmind]\n',destination)
            original={p.name:p.read_bytes() for p in destination.iterdir()}
            for row in result['files']:
                self.assertEqual(hashlib.sha256(original[row['file']]).hexdigest(),row['sha256'])
            with self.assertRaises(FileExistsError):prepare(self.profile,b'',destination)
            self.assertEqual({p.name:p.read_bytes() for p in destination.iterdir()},original)


if __name__=='__main__':unittest.main()
