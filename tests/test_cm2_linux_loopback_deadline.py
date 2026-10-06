"""Actual slow headers/body cannot keep the private HTTP request alive."""
import socketserver
import threading
import time

import pytest
from test_cm2_linux_loopback_transport import product, headers


@pytest.mark.parametrize('stage', ['headers', 'body'])
def test_actual_slow_wire_bytes_share_one_total_deadline(stage):
    module = product(); private = headers(); calls = []
    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.settimeout(1)
            raw = b''
            while not raw.endswith(b'\r\n\r\n') and len(raw) < 8192:
                chunk = self.request.recv(1024)
                if not chunk: return
                raw += chunk
            calls.append(True)
            try:
                if stage == 'headers':
                    self.request.sendall(b'HTTP/1.1 200 OK\r\nX-Drip: ')
                else:
                    self.request.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 30\r\n\r\n')
                for _ in range(30):
                    self.request.sendall(b'x'); time.sleep(.04)
                if stage == 'headers': self.request.sendall(b'\r\nContent-Length: 0\r\n\r\n')
            except OSError: pass
    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True
        def handle_error(self, *args): pass
    server = Server(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
    thread.start()
    try:
        origin = 'http://127.0.0.1:' + str(server.server_address[1]); start = time.monotonic()
        with pytest.raises(module.ProvisionRefusal) as error:
            module.LoopbackTransport(origin).get(origin + '/health/ready', headers=private, timeout=.12, max_bytes=65536)
        assert time.monotonic() - start < .4
        assert error.value.code == 'cortex_health_unavailable' and calls == [True]
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=1)
