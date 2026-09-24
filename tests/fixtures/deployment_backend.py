"""Deterministic HTTP/SSE/upload/WebSocket upstream for real proxy tests."""
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import time

SLOT = os.environ['SLOT']


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def reply(self, body, status=200):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == '/ready':
            self.reply({'slot': SLOT}, 503 if Path('/tmp/unhealthy').exists() else 200)
        elif self.path == '/stream':
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Connection', 'close')
            self.end_headers()
            for i in range(30):
                self.wfile.write(f'data: {SLOT}:{i}\n\n'.encode())
                self.wfile.flush()
                time.sleep(0.1)
            self.close_connection = True
        elif self.path == '/ws':
            key = self.headers['Sec-WebSocket-Key'] + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
            self.send_response(101)
            self.send_header('Upgrade', 'websocket')
            self.send_header('Connection', 'Upgrade')
            self.send_header('Sec-WebSocket-Accept', base64.b64encode(hashlib.sha1(key.encode()).digest()).decode())
            self.end_headers()
            for i in range(30):
                data = f'{SLOT}:{i}'.encode()
                self.wfile.write(bytes([0x81, len(data)]) + data)
                self.wfile.flush()
                time.sleep(0.1)
            self.wfile.write(b'\x88\x02\x03\xe8')
            self.wfile.flush()
            self.close_connection = True
        else:
            self.reply({'slot': SLOT})

    def do_POST(self):
        remaining = int(self.headers['Content-Length'])
        digest = hashlib.sha256()
        while remaining:
            chunk = self.rfile.read(min(8192, remaining))
            if not chunk:
                return
            remaining -= len(chunk)
            digest.update(chunk)
        self.reply({'slot': SLOT, 'sha256': digest.hexdigest()})


ThreadingHTTPServer(('0.0.0.0', 8000), Handler).serve_forever()
