"""Offline recovery asset contracts; not native provider/mailbox acceptance."""
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class RecoveryAssetTests(unittest.TestCase):
    def test_baseline_stays_disabled_until_provider_qualification(self):
        realm = json.loads((ROOT / 'deploy/keycloak/graphmind-realm.template.json').read_text('utf-8'))
        self.assertFalse(realm['resetPasswordAllowed'])

    def test_activation_uses_guard_force_login_and_keeps_mfa(self):
        policy = json.loads((ROOT / 'deploy/keycloak/graphmind-recovery.json').read_text('utf-8'))
        self.assertEqual(policy['keycloakVersion'], '26.7.3')
        self.assertEqual(policy['realmSettings']['resetCredentialsFlow'], policy['flow']['alias'])
        self.assertEqual(policy['realmSettings']['attributes']['actionTokenGeneratedByUserLifespan.reset-credentials'], '900')
        self.assertEqual(policy['eventListener'], 'graphmind-password-revocation')
        self.assertEqual(policy['authenticatorConfig']['config']['force-login'], 'true')
        executions = policy['flow']['authenticationExecutions']
        self.assertEqual([item['authenticator'] for item in executions],
                         ['graphmind-reset-existing-password', 'reset-credential-email', 'reset-password'])
        self.assertTrue(all(item['requirement'] == 'REQUIRED' for item in executions))
        self.assertEqual(executions[1]['authenticatorConfig'], policy['authenticatorConfig']['alias'])

    def test_spi_services_are_explicit(self):
        services = ROOT / 'deploy/keycloak/recovery-src/META-INF/services'
        for filename, factory in [('org.keycloak.authentication.AuthenticatorFactory', 'ExistingPasswordResetFactory'),
                                  ('org.keycloak.events.EventListenerProviderFactory', 'PasswordRevocationFactory')]:
            self.assertEqual((services / filename).read_text('utf-8').strip(), 'org.graphmind.keycloak.' + factory)

    def test_email_copy_inherits_native_templates_and_dynamic_security_inputs(self):
        theme = ROOT / 'deploy/keycloak/themes/graphmind/email'
        self.assertIn('parent=base', (theme / 'theme.properties').read_text('utf-8'))
        messages = dict(line.split('=', 1) for line in
                        (theme / 'messages/messages_en.properties').read_text('utf-8').splitlines()
                        if line and not line.startswith('#'))
        self.assertEqual(set(messages), {kind + suffix for kind in ('passwordReset', 'emailVerification')
                                        for suffix in ('Subject', 'Body', 'BodyHtml')})
        self.assertEqual(messages['passwordResetSubject'], 'GraphMind - Reset your password')
        self.assertEqual(messages['emailVerificationSubject'], 'GraphMind - Verify your email address')
        for kind in ('passwordReset', 'emailVerification'):
            for suffix in ('Body', 'BodyHtml'):
                body = messages[kind + suffix]
                self.assertIn('{0}', body)  # Unmodified native one-time URL.
                self.assertIn('{3}', body)  # Native formatted lifespan, not a hardcoded 15.
                self.assertIn('{2}', body)  # Native realm branding.
                self.assertIn('can be used only once', body)
                self.assertIn("didn''t", body)  # Java MessageFormat apostrophe escape.
                self.assertNotIn('https://', body)
                self.assertNotIn('15 minutes', body)
                self.assertNotIn('credentials', body)
                self.assertNotIn('<script', body)
        self.assertFalse((theme / 'html').exists())
        self.assertFalse((theme / 'text').exists())


if __name__ == '__main__':
    unittest.main()
