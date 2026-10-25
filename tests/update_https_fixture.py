"""Real localhost TLS only; private TLS keys live in disposable external fixtures."""
from datetime import datetime, timedelta, timezone
import http.server
import ipaddress
from pathlib import Path
import ssl
import tempfile
import threading
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


class HTTPSFixture:
    def __init__(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='MediaFlow-online-https-')
        self.directory = Path(self.temporary.name)
        self.routes = {}
        self.requests = []
        self.stopped = threading.Event()
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'MediaFlow localhost TEST ONLY')])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now-timedelta(minutes=5)).not_valid_after(now+timedelta(days=1))
                .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost'),
                    x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
                .sign(key, hashes.SHA256()))
        cert_bytes = cert.public_bytes(serialization.Encoding.PEM)
        (self.directory/'certificate.pem').write_bytes(cert_bytes)
        (self.directory/'test-tls-key.pem').write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        fixture = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def do_GET(self):
                route = urlsplit(self.path).path
                fixture.requests.append(route)
                value = fixture.routes.get(route, (404, b'not found'))
                try:
                    if callable(value):
                        value(self)
                        return
                    status, data, *extra = value if isinstance(value, tuple) else (200, value)
                    self.send_response(status)
                    for key, val in dict(extra[0] if extra else {}).items():
                        self.send_header(key, val)
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (OSError, ValueError):
                    pass

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.minimum_version = ssl.TLSVersion.TLSv1_2
        server_context.load_cert_chain(self.directory/'certificate.pem', self.directory/'test-tls-key.pem')
        self.server.socket = server_context.wrap_socket(self.server.socket, server_side=True)
        self.context = ssl.create_default_context(cadata=cert_bytes.decode('ascii'))
        self.url = f'https://localhost:{self.server.server_port}'
        self.thread = threading.Thread(target=lambda:self.server.serve_forever(poll_interval=.02), daemon=True)
        self.thread.start()

    def close(self):
        self.stopped.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
