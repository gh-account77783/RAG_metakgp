"""Container deployment contracts; these tests do not run Docker or models."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import tomllib
import unittest

import yaml

from graphmind.config import Settings
from tests.support.paths import REPOSITORY
from tools.docker_configuration import BUILD_INPUTS, prepare, render
from tools.identity_configuration import render_realm

DEPLOY = REPOSITORY / 'deploy/docker'


def module(name):
    spec = importlib.util.spec_from_file_location('docker_' + name, DEPLOY / (name + '.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class DockerConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name).resolve()
        self.root = root
        for relative in ('bundle', 'models/ollama', 'models/visual', 'imports'):
            (root / relative).mkdir(parents=True)
        manifest = {'target': {'implementation': 'CPython', 'python': '3.13', 'platform': 'linux-x86_64'},
                    'extras': ['tree', 'visual']}
        (root / 'bundle/manifest.json').write_text(json.dumps(manifest))
        smtp = {'host': 'smtp.example.test', 'port': '587', 'from': 'sender@example.test',
                'auth': 'true', 'user': 'sender@example.test', 'password': 'test-smtp-password',
                'starttls': 'true', 'ssl': 'false'}
        (root / 'smtp.json').write_text(json.dumps(smtp))
        self.profile = json.loads((DEPLOY / 'profile.example.json').read_text())
        self.profile.update(bundle_dir=str(root / 'bundle'), models_dir=str(root / 'models'),
            imports_dir=str(root / 'imports'), smtp_file=str(root / 'smtp.json'),
            bundle_manifest_sha256=hashlib.sha256((root / 'bundle/manifest.json').read_bytes()).hexdigest(),
            secret_gid=os.getgid() if hasattr(os, 'getgid') and os.getgid() else 1000)

    def test_generated_config_uses_existing_settings_and_separates_reader_admin_bootstrap(self):
        output = self.root / 'new-private'
        report = prepare(self.profile, output)
        settings = Settings.load(config_path=output / 'reader.toml', dotenv_path=output / 'reader.env', environ={})
        settings.validate(require_identity_secrets=True, require_neo4j_secret=True)
        self.assertEqual(settings.enabled_engines, ('graphmind', 'multimodal', 'tree_navigation'))
        self.assertEqual((settings.chroma_host, settings.chroma_mode), ('127.0.0.1', 'http'))
        self.assertEqual(settings.answer_mode, 'local')
        self.assertTrue(settings.browser_cookie_secure)
        self.assertEqual(settings.answer_max_retries, 0)
        reader = (output / 'reader.env').read_text()
        admin = (output / 'host-admin.env').read_text()
        self.assertNotIn('ADMIN_CLIENT_SECRET', reader)
        self.assertIn('ADMIN_CLIENT_SECRET', admin)
        self.assertNotIn('FLOW_SIGNING_KEY', admin)
        self.assertNotIn('BOOTSTRAP', reader + admin)
        self.assertNotIn(settings.identity_browser_client_secret, json.dumps(report))
        self.assertNotIn(settings.neo4j_password, (output / 'compose.env').read_text())

    def test_canonical_realm_preserves_pkce_verification_and_scoped_service_account(self):
        files = render(self.profile, self.root / 'new')
        realm = json.loads(files['realm-import.json'])
        self.assertTrue(realm['verifyEmail'])
        self.assertTrue(realm['registrationAllowed'])
        self.assertEqual(len(realm['users']), 1)
        self.assertNotIn('credentials', realm['users'][0])
        self.assertEqual(realm['users'][0]['clientRoles']['realm-management'],
                         ['query-users', 'view-users', 'manage-users'])
        self.assertFalse(any(client.get('directAccessGrantsEnabled') for client in realm['clients']))
        client = next(client for client in realm['clients'] if client['clientId'] == 'graphmind-web')
        self.assertEqual(client['attributes']['pkce.code.challenge.method'], 'S256')
        self.assertEqual(client['redirectUris'], ['https://rag.example.com/auth/callback'])
        self.assertEqual(realm['smtpServer']['starttls'], 'true')
        self.assertFalse(realm['identityProviders'][0]['enabled'])

    def test_renderer_does_not_treat_private_values_as_json_source(self):
        result = render_realm('{"value":"${VALUE}"}', {'VALUE': 'quote" and \\ slash'})
        self.assertEqual(result['value'], 'quote" and \\ slash')

    def test_retry_preserves_existing_secrets_and_only_public_build_inputs_are_copied(self):
        output = self.root / 'new'
        report = prepare(self.profile, output)
        before = (output / 'reader.env').read_bytes()
        with self.assertRaises(FileExistsError):
            prepare(self.profile, output)
        self.assertEqual((output / 'reader.env').read_bytes(), before)
        copied = {path.relative_to(output / 'build-source').as_posix()
                  for path in (output / 'build-source').rglob('*') if path.is_file()}
        self.assertEqual(copied, set(BUILD_INPUTS))
        self.assertEqual(set(report['public_build_inputs']), copied)

    def test_unsafe_hosts_paths_group_and_hash_fail_before_output_creation(self):
        for key, value in (('reader_host', 'rag.example.com/path'), ('identity_host', self.profile['reader_host']),
                           ('secret_gid', 0), ('bundle_manifest_sha256', '0' * 64),
                           ('answer_model', 'name\nmalformed'), ('imports_dir', self.profile['models_dir'])):
            profile = {**self.profile, key: value}
            with self.subTest(key=key), self.assertRaises(ValueError):
                prepare(profile, self.root / 'rejected')
            self.assertFalse((self.root / 'rejected').exists())
        with self.assertRaises(ValueError):
            prepare(self.profile, self.root / '$PRIVATE')

    def test_foreign_bundle_missing_models_and_cleartext_smtp_are_rejected(self):
        for change in ('foreign', 'models', 'smtp'):
            with self.subTest(change=change):
                manifest = {'target': {'implementation': 'CPython', 'python': '3.13',
                                      'platform': 'win-amd64' if change == 'foreign' else 'linux-x86_64'},
                            'extras': ['tree', 'visual']}
                (self.root / 'bundle/manifest.json').write_text(json.dumps(manifest))
                profile = {**self.profile, 'bundle_manifest_sha256': hashlib.sha256(
                    (self.root / 'bundle/manifest.json').read_bytes()).hexdigest()}
                if change == 'models':
                    profile['retrieval_profile'] = 'hybrid'
                if change == 'smtp':
                    smtp = json.loads((self.root / 'smtp.json').read_text())
                    smtp['starttls'] = 'false'
                    (self.root / 'smtp.json').write_text(json.dumps(smtp))
                with self.assertRaises(ValueError):
                    prepare(profile, self.root / 'rejected')
                self.assertFalse((self.root / 'rejected').exists())

    def test_google_is_enabled_only_from_explicit_private_configuration(self):
        path = self.root / 'google.json'
        path.write_text(json.dumps({'clientId': 'test-client', 'clientSecret': 'test-secret'}))
        files = render({**self.profile, 'google_file': str(path)}, self.root / 'new')
        provider = json.loads(files['realm-import.json'])['identityProviders'][0]
        self.assertTrue(provider['enabled'])
        self.assertEqual(provider['config']['clientId'], 'test-client')
        self.assertNotIn(b'test-secret', files['reader.env'] + files['compose.env'])


class DockerTopologyTests(unittest.TestCase):
    def test_loopback_boundary_persistence_and_secret_access_are_preserved(self):
        compose = yaml.safe_load((DEPLOY / 'compose.yaml').read_text())
        services = compose['services']
        self.assertEqual([name for name, service in services.items() if service.get('ports')], ['gateway'])
        for name, service in services.items():
            with self.subTest(service=name):
                self.assertFalse(service.get('privileged', False))
                if name != 'gateway':
                    self.assertEqual(service['network_mode'], 'service:gateway')
                    for unsupported in ('extra_hosts', 'hostname', 'dns', 'dns_search', 'ports', 'expose', 'networks'):
                        self.assertNotIn(unsupported, service)
                self.assertTrue(all(not isinstance(volume, list) for volume in service.get('volumes', [])))
        self.assertEqual(services['reader']['secrets'], ['reader_env'])
        self.assertEqual(services['worker']['secrets'], ['reader_env'])
        self.assertEqual(services['admin']['secrets'], ['host_admin_env'])
        for role in ('reader', 'worker', 'admin'):
            self.assertIn('app_data:/var/lib/graphmind', services[role]['volumes'])
            models = next(volume for volume in services[role]['volumes'] if isinstance(volume, dict) and volume['target'] == '/models')
            self.assertTrue(models['read_only'])
        self.assertEqual(services['proxy']['depends_on']['identity-setup']['condition'], 'service_completed_successfully')
        self.assertEqual(services['admin']['profiles'], ['tools'])
        self.assertEqual(len(services['gateway']['extra_hosts']), 2)

    def test_startup_graph_has_no_cycle_and_no_backend_port_is_published(self):
        services = yaml.safe_load((DEPLOY / 'compose.yaml').read_text())['services']
        def visit(name, stack):
            self.assertNotIn(name, stack, 'Startup cycle')
            dependencies = set(services[name].get('depends_on', {}))
            if services[name].get('network_mode', '').startswith('service:'):
                dependencies.add(services[name]['network_mode'].split(':', 1)[1])
            for dependency in dependencies:
                self.assertIn(dependency, services)
                visit(dependency, {*stack, name})
        for name in services:
            visit(name, set())
        ports = services['gateway']['ports']
        self.assertTrue(all(port.endswith((':80:80', ':443:443')) for port in ports))

    def test_container_commands_reuse_cli_and_reject_reader_command_injection(self):
        launcher = module('runtime')
        reader = launcher.command(['reader'])
        self.assertIn('graphmind.cli', reader)
        self.assertEqual(reader[-5:], ['serve', '--host', '127.0.0.1', '--port', '18000'])
        self.assertIn('/run/secrets/reader_env', launcher.command(['worker']))
        admin = launcher.command(['admin', 'accounts', 'list'])
        self.assertIn('/run/secrets/host_admin_env', admin)
        self.assertEqual(admin[-2:], ['accounts', 'list'])
        for values in ([], ['reader', 'accounts', 'delete'], ['unknown']):
            with self.subTest(values=values), self.assertRaises(ValueError):
                launcher.command(values)


class FakeIdentity:
    def __init__(self):
        self.realm = {'realm': 'graphmind', 'attributes': {'retained': 'value'}, 'eventsListeners': ['jboss-logging']}
        self.profile = {}
        self.flows = []
        self.executions = []
        self.configs = {}
        self.bad_profile = False

    def request(self, method, path, payload=None):
        api = module('bootstrap_identity')
        if path == api.REALM:
            if method == 'PUT': self.realm.update(copy.deepcopy(payload))
            return copy.deepcopy(self.realm)
        if path.endswith('/users/profile'):
            if method == 'PUT': self.profile = copy.deepcopy(payload)
            result = copy.deepcopy(self.profile)
            if self.bad_profile:
                result['attributes'][-1]['permissions']['edit'] = ['admin', 'user']
            return result
        if path.endswith('/authentication/flows'):
            if method == 'POST': self.flows.append(copy.deepcopy(payload))
            return copy.deepcopy(self.flows)
        if path.endswith('/executions/execution'):
            self.executions.append({'id': str(len(self.executions)), 'providerId': payload['provider']})
            return None
        if '/authentication/executions/' in path and path.endswith('/config'):
            identifier = path.split('/')[-2]
            self.configs[identifier] = copy.deepcopy(payload)
            next(row for row in self.executions if row['id'] == identifier)['authenticationConfig'] = identifier
            return None
        if '/authentication/config/' in path:
            return copy.deepcopy(self.configs[path.split('/')[-1]])
        if path.endswith('/executions'):
            if method == 'PUT':
                next(row for row in self.executions if row['id'] == payload['id']).update(payload)
            return copy.deepcopy(self.executions)
        raise AssertionError('Unexpected identity endpoint')


class DockerIdentityBootstrapTests(unittest.TestCase):
    def test_bootstrap_activates_canonical_policy_idempotently_and_preserves_existing_state(self):
        api = FakeIdentity()
        profile = json.loads((REPOSITORY / 'deploy/keycloak/graphmind-user-profile.json').read_text())
        policy = json.loads((REPOSITORY / 'deploy/keycloak/graphmind-recovery.json').read_text())
        bootstrap = module('bootstrap_identity')
        for _ in range(2):
            result = bootstrap.activate(api, profile, policy)
            self.assertTrue(result['configured'])
        self.assertEqual(len(api.flows), 1)
        self.assertEqual(len(api.executions), 3)
        self.assertEqual(api.realm['attributes']['retained'], 'value')
        self.assertEqual(api.realm['eventsListeners'], ['jboss-logging', 'graphmind-password-revocation'])
        self.assertEqual(api.realm['emailTheme'], 'graphmind')

    def test_failed_profile_readback_does_not_activate_recovery_or_expose_reader(self):
        api = FakeIdentity()
        api.bad_profile = True
        profile = json.loads((REPOSITORY / 'deploy/keycloak/graphmind-user-profile.json').read_text())
        policy = json.loads((REPOSITORY / 'deploy/keycloak/graphmind-recovery.json').read_text())
        with self.assertRaises(ValueError): module('bootstrap_identity').activate(api, profile, policy)
        self.assertFalse(api.flows)
        self.assertNotIn('resetPasswordAllowed', api.realm)

    def test_bootstrap_transport_refuses_redirects_and_other_realms(self):
        bootstrap = module('bootstrap_identity')
        self.assertIsNone(bootstrap.NoRedirects().redirect_request(None, None, 302, '', {}, 'https://elsewhere.example'))
        api = object.__new__(bootstrap.AdminAPI)
        with self.assertRaises(ValueError): api.request('GET', 'admin/realms/master')
