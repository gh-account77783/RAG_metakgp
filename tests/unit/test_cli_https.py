"""Direct HTTPS configuration must fail before storage startup and retain one ASGI boundary."""
import io
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest.mock import Mock, patch

from graphmind import cli


class HttpsCliTests(unittest.TestCase):
    def test_incomplete_pair_or_http_identity_fails_before_application_startup(self):
        settings=Mock(public_base_url='http://127.0.0.1:8000')
        for args in (['--ssl-certfile','cert.pem'],['--ssl-keyfile','key.pem'],
                     ['--ssl-certfile','cert.pem','--ssl-keyfile','key.pem']):
            with self.subTest(args=args),patch.object(cli,'_settings',return_value=settings),patch.object(
                    cli,'GraphMindApplication') as application,redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:cli.main(['serve',*args])
                self.assertEqual(error.exception.code,2);application.assert_not_called()

    def test_unusable_chain_reports_sanitized_error_before_application_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            cert=Path(directory)/'certificate.pem';key=Path(directory)/'private-key.pem'
            cert.write_text('invalid certificate contents');key.write_text('private test marker')
            output=io.StringIO()
            with patch.object(cli,'_settings',return_value=Mock(public_base_url='https://localhost')),patch.object(
                    cli,'GraphMindApplication') as application,redirect_stderr(output):
                with self.assertRaises(SystemExit) as error:
                    cli.main(['serve','--ssl-certfile',str(cert),'--ssl-keyfile',str(key)])
            self.assertEqual(error.exception.code,2);application.assert_not_called()
            self.assertIn('could not be loaded',output.getvalue())
            for private in (str(key),str(cert),'private test marker'):self.assertNotIn(private,output.getvalue())

    def test_direct_https_and_proxy_modes_delegate_to_the_same_server(self):
        settings=Mock(public_base_url='https://localhost')
        for tls in ([],['--ssl-certfile','cert.pem','--ssl-keyfile','key.pem']):
            with self.subTest(tls=tls),patch.object(cli,'_settings',return_value=settings),patch.object(
                    cli,'GraphMindApplication'),patch('graphmind.product_server.create_product_server',return_value=(None,'shared-asgi')),patch(
                    'uvicorn.run') as run,patch.object(cli.ssl,'SSLContext'):
                cli.main(['serve',*tls])
                self.assertEqual(run.call_args.args,('shared-asgi',))
                self.assertFalse(run.call_args.kwargs['access_log'])
                self.assertEqual(run.call_args.kwargs.get('ssl_keyfile'),'key.pem' if tls else None)
