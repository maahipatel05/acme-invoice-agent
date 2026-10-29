"""The console's real HTTP handler: security checks, async processing and review."""
import io
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from acme.server import make_handler
from acme.store import Store

ROOT = Path(__file__).resolve().parents[1]


class MemorySocket:
    def __init__(self, data):
        self.input, self.output = io.BytesIO(data), io.BytesIO()

    def makefile(self, *args, **kwargs):
        return self.input

    def sendall(self, data):
        self.output.write(data)


class HandlerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'a.db')
        self.handler = make_handler(self.store)

    def request(self, path, method='GET', body=b'', headers=None, host='127.0.0.1:8080'):
        lines = [f'{method} {path} HTTP/1.0', f'Host: {host}', f'Content-Length: {len(body)}']
        lines += [f'{k}: {v}' for k, v in (headers or {}).items()]
        sock = MemorySocket(('\r\n'.join(lines) + '\r\n\r\n').encode() + body)
        self.handler(sock, ('127.0.0.1', 1), object())
        head, content = sock.output.getvalue().split(b'\r\n\r\n', 1)
        return int(head.split()[1]), content

    def wait(self, run_id):
        for _ in range(200):
            run = self.store.get(run_id)
            if run['status'] != 'processing':
                return run
            time.sleep(0.02)
        self.fail('run did not finish')

    def test_upload_processes_in_background_then_review_pays(self):
        token = json.loads(self.request('/api/runs')[1])['token']
        source = (ROOT / 'data/scenarios/high_value.json').read_bytes()
        self.assertEqual(self.request('/api/process', 'POST', source)[0], 403)          # no CSRF token
        code, content = self.request('/api/process', 'POST', source, {'X-CSRF-Token': token, 'X-Filename': 'high%20value.json'})
        self.assertEqual(code, 202)
        started = json.loads(content)
        self.assertEqual(started['filename'], 'high value.json')
        run = self.wait(started['id'])
        self.assertEqual(run['status'], 'pending_review')
        code, content = self.request('/api/process', 'POST', source, {'X-CSRF-Token': token})
        self.assertEqual((code, json.loads(content)['replayed']), (200, True))
        args = json.dumps({'id': run['id'], 'decision': 'approve', 'reviewer': 'VP', 'reason': 'PO matched'}).encode()
        code, content = self.request('/api/review', 'POST', args, {'X-CSRF-Token': token})
        self.assertEqual((code, json.loads(content)['status']), (200, 'paid'))
        self.assertEqual(self.request('/api/review', 'POST', args, {'X-CSRF-Token': token})[0], 400)

    def test_malformed_requests_are_rejected_cleanly(self):
        token = json.loads(self.request('/api/runs')[1])['token']
        headers = {'X-CSRF-Token': token}
        self.assertEqual(self.request('/api/review', 'POST', b'{not json', headers)[0], 400)
        self.assertEqual(self.request('/api/review', 'POST', b'{"id": "nope"}', headers)[0], 400)
        self.assertEqual(self.request('/api/nothing', 'POST', b'{}', headers)[0], 404)
        self.assertEqual(self.request('/api/process', 'POST', b'x', {'X-CSRF-Token': 'forged'})[0], 403)
        code, content = self.request('/api/process', 'POST', b'not an invoice', {**headers, 'X-Filename': 'a.exe'})
        self.assertEqual(code, 202)
        self.assertEqual(self.wait(json.loads(content)['id'])['status'], 'rejected')

    def test_failed_run_can_be_released_from_the_console(self):
        token = json.loads(self.request('/api/runs')[1])['token']
        headers = {'X-CSRF-Token': token}
        source = (ROOT / 'data/scenarios/clean.json').read_bytes()
        from acme.workflow import Workflow
        result, _ = Workflow(self.store).start('clean.json', source)       # simulate a crash mid-run
        self.assertEqual(self.store.get(result['id'])['status'], 'processing')
        code, content = self.request('/api/recover', 'POST', json.dumps({'id': result['id']}).encode(), headers)
        self.assertEqual((code, json.loads(content)['status']), (200, 'error'))
        code, content = self.request('/api/process', 'POST', source, {**headers, 'X-Filename': 'clean.json'})
        self.assertEqual(code, 202)
        paid = self.wait(json.loads(content)['id'])
        self.assertEqual(paid['status'], 'paid')
        self.assertEqual(self.request('/api/recover', 'POST', json.dumps({'id': paid['id']}).encode(), headers)[0], 400)

    def test_host_path_and_size_guards(self):
        self.assertEqual(self.request('/api/runs', host='evil.example')[0], 403)
        self.assertEqual(self.request('/../../.env')[0], 404)
        token = json.loads(self.request('/api/runs')[1])['token']
        self.assertEqual(self.request('/api/process', 'POST', b'', {'X-CSRF-Token': token})[0], 413)
        code, content = self.request('/', host='localhost:8080')
        self.assertEqual(code, 200)
        self.assertIn(b'From inbox to decision', content)


class SocketTests(unittest.TestCase):
    def test_real_server_round_trip(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp) / 'a.db')
            try:
                server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(store))
            except PermissionError:
                self.skipTest('sandbox forbids loopback sockets')
            port = server.server_address[1]
            server.RequestHandlerClass = make_handler(store, port=port)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f'http://127.0.0.1:{port}'
                try:
                    with urllib.request.urlopen(base + '/api/runs') as response:
                        token = json.load(response)['token']
                except (urllib.error.URLError, PermissionError):
                    self.skipTest('sandbox forbids loopback client connections')
                payload = (ROOT / 'data/invoices/invoice_1001.txt').read_bytes()
                req = urllib.request.Request(base + '/api/process', payload, {'X-CSRF-Token': token, 'X-Filename': 'invoice_1001.txt'})
                with urllib.request.urlopen(req) as response:
                    run_id = json.load(response)['id']
                for _ in range(200):
                    with urllib.request.urlopen(base + '/api/runs') as response:
                        run = next(r for r in json.load(response)['runs'] if r['id'] == run_id)
                    if run['status'] != 'processing':
                        break
                    time.sleep(0.02)
                self.assertEqual(run['status'], 'paid')
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
