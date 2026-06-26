#!/usr/bin/env python3
"""NBA Dashboard server — serves files + accepts POST writes to output/ only."""
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
import os, sys

class NBAHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        # Serve dashboard as the root page
        if self.path in ('/', '/index.html'):
            self.path = '/dashboard/index.html'
        super().do_GET()

    def _send(self, code, body=b''):
        self.send_response(code)
        self.send_header('Content-Type', 'text/plain')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'close')
        self.end_headers()
        if body:
            self.wfile.write(body)
        self.wfile.flush()

    def do_OPTIONS(self):
        self._send(200)

    def do_POST(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length)
            path = os.path.abspath(self.translate_path(self.path))
            allowed = os.path.abspath(os.path.join('.', 'output'))
            if not path.startswith(allowed):
                self._send(403, b'Forbidden: only output/ writes permitted'); return
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'wb') as f:
                f.write(body)
            self._send(200, b'OK')
        except Exception as ex:
            self._send(500, str(ex).encode())

    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET,POST,OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        super().end_headers()

    def log_message(self, fmt, *args):
        pass  # suppress request logs

if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    print(f'CareIntel NBA server: http://localhost:{port}/dashboard/index.html')
    ThreadingHTTPServer(('', port), NBAHandler).serve_forever()
