"""Loopback-only review console. No third-party assets or hosted services.

Uploads return immediately; the agents run on a worker thread and checkpoint each
step, which the console polls so reviewers watch the workflow progress live.
"""
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse
from .domain import InputError
from .gemini import Gemini, ModelError
from .ingestion import MAX_BYTES
from .workflow import Workflow

ASSETS = {'/': ('index.html', 'text/html; charset=utf-8'), '/app.js': ('app.js', 'text/javascript'),
          '/style.css': ('style.css', 'text/css')}


def make_handler(store, mode='offline', port=8080):
    token = secrets.token_urlsafe(32)

    def workflow():
        return Workflow(store, Gemini() if mode == 'gemini' else None)

    class Handler(BaseHTTPRequestHandler):
        def respond(self, code, body, content_type='application/json'):
            data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; "
                                                        "connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)

        def valid_host(self):
            hosts = self.headers.get_all('Host', [])
            return len(hosts) == 1 and hosts[0] in (f'127.0.0.1:{port}', f'localhost:{port}')

        def do_GET(self):
            if not self.valid_host():
                return self.respond(403, {'error': 'Invalid host'})
            path = urlparse(self.path).path
            if path == '/api/runs':
                return self.respond(200, {'runs': store.all(), 'token': token, 'mode': mode,
                                          'model': Gemini().model if mode == 'gemini' else None})
            if path in ASSETS:
                filename, kind = ASSETS[path]
                return self.respond(200, (Path(__file__).parent / 'static' / filename).read_text(), kind)
            return self.respond(404, {'error': 'Not found'})

        def do_POST(self):
            if not self.valid_host() or self.headers.get('X-CSRF-Token') != token:
                return self.respond(403, {'error': 'Reload the page before making changes'})
            try:
                length = int(self.headers.get('Content-Length', '-1'))
                if not 0 < length <= MAX_BYTES:
                    return self.respond(413, {'error': 'Upload is empty or exceeds 5 MB'})
                body = self.rfile.read(length)
                path = urlparse(self.path).path
                if path == '/api/process':
                    filename = unquote(self.headers.get('X-Filename', 'invoice.txt'))
                    flow = workflow()
                    result, new = flow.start(filename, body)
                    if new:
                        threading.Thread(target=flow.run, args=(result, filename, body), daemon=True).start()
                    return self.respond(202 if new else 200, {**result, 'replayed': not new})
                args = json.loads(body)
                if path == '/api/review':
                    return self.respond(200, workflow().review(args['id'], args['decision'], args['reviewer'], args['reason']))
                if path == '/api/recover':
                    return self.respond(200, Workflow(store).recover(args['id']))
                return self.respond(404, {'error': 'Not found'})
            except (InputError, ModelError, KeyError, ValueError, TypeError) as exc:
                return self.respond(400, {'error': str(exc)})
            except Exception:
                return self.respond(500, {'error': 'Local server error'})

        def log_message(self, fmt, *args):
            pass
    return Handler


def serve(store, mode, port, bind='127.0.0.1'):
    # Loopback by default. Docker passes 0.0.0.0 so the published port works; the Host check
    # above still only accepts localhost names, which keeps DNS-rebinding protection.
    server = ThreadingHTTPServer((bind, port), make_handler(store, mode, port))
    print(f'Invoice console: http://127.0.0.1:{port}  ({mode} mode, simulated payments only)', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
