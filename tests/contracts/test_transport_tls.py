"""Both HTTPS transports verify trust and hostname using the shared bundle."""
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
import ipaddress
from pathlib import Path
import ssl
import tempfile
import threading
import unittest
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from graphmind.errors import IdentityUnavailableError
from graphmind.http_client import HttpUnavailableError, UrllibJsonTransport
from graphmind.identity import UrllibIdentityTransport
from graphmind.tls import verified_context


class TransportTLSTests(unittest.TestCase):
    def test_shared_trust_accepts_owned_server_and_rejects_unknown_ca_or_wrong_host(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Owned TLS fixture')])
            now = datetime.now(UTC)
            certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now-timedelta(minutes=1)).not_valid_after(now+timedelta(hours=1))
                .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
                .sign(key, hashes.SHA256()))
            cert_path, key_path = root/'certificate.pem', root/'owned-key.pem'
            cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
            key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_response(200); self.end_headers(); self.wfile.write(b'{"owned":true}')
                def log_message(self, *_): pass
            server = HTTPServer(('127.0.0.1', 0), Handler)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert_path, key_path)
            server.socket = context.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            transports = [(UrllibJsonTransport(), HttpUnavailableError),
                          (UrllibIdentityTransport(), IdentityUnavailableError)]
            port = server.server_port
            try:
                with patch('graphmind.tls.certifi.where', return_value=str(cert_path)):
                    verified_context.cache_clear()
                    for transport, error in transports:
                        self.assertEqual(transport.request_json('GET', f'https://127.0.0.1:{port}/',
                            headers={}, payload=None, timeout=3), {'owned': True})
                        with self.assertRaises(error):
                            transport.request_json('GET', f'https://localhost:{port}/',
                                headers={}, payload=None, timeout=3)
                verified_context.cache_clear()
                for transport, error in transports:
                    with self.assertRaises(error):
                        transport.request_json('GET', f'https://127.0.0.1:{port}/',
                            headers={}, payload=None, timeout=3)
            finally:
                verified_context.cache_clear()
                server.shutdown(); server.server_close(); thread.join(3)
