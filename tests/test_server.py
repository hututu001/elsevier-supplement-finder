import http.client
import json
import threading
import time
import unittest
from unittest.mock import patch

from finder import downloads, search
from finder.server import Server
from test_finder import PII, Response


class WebTests(unittest.TestCase):
    def setUp(self):
        self.server = Server(('127.0.0.1', 0))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(2)

    def request(self, path, body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        supplied = {'Content-Type': 'application/json', 'X-Local-Request': '1', **(headers or {})}
        connection.request('POST' if body is not None else 'GET', path, json.dumps(body) if body is not None else None, supplied)
        response = connection.getresponse(); data = response.read(); result = response.status, dict(response.headers), data
        connection.close(); return result

    def test_assets_settings_and_health_without_external_dependencies(self):
        for path in ['/', '/app.js', '/style.css', '/images/supplement-documents.svg', '/images/supplement-formats.svg', '/api/health']:
            status, headers, data = self.request(path)
            self.assertEqual(status, 200, path)
            self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
            self.assertIn("default-src 'self'", headers['Content-Security-Policy'])
        settings = json.loads(self.request('/api/settings')[2])
        self.assertEqual(settings['concurrency'], 32)
        self.assertNotIn('jpeg', settings['defaultExtensions'])
        self.assertEqual(self.request('/finder/server.py')[0], 404)

    def test_local_origin_and_host_checks_protect_mutations(self):
        body = {'query': PII}
        for headers in [{'Origin': 'https://evil.test'}, {'X-Local-Request': ''}, {'Host': 'attacker.test'}]:
            self.assertEqual(self.request('/api/search', body, headers)[0], 403)
        self.assertEqual(self.request('/api/search', {'query': PII, 'extensions': []})[0], 400)

    def test_public_assets_with_injected_origin_keep_correct_mime_types(self):
        for origin in ('null', '', 'chrome-extension://test', 'http://127.0.0.1:13006'):
            for path, mime in (('/', 'text/html'), ('/style.css', 'text/css'), ('/app.js', 'text/javascript'),
                               ('/images/logo.svg', 'image/svg+xml'), ('/images/supplement-documents.svg', 'image/svg+xml'),
                               ('/images/supplement-formats.svg', 'image/svg+xml'), ('/api/settings', 'application/json')):
                with self.subTest(origin=origin, path=path):
                    status, headers, body = self.request(path, headers={'Origin': origin})
                    self.assertEqual(status, 200)
                    self.assertEqual(headers['Content-Type'], mime + '; charset=utf-8')
                    self.assertTrue(body)
                    self.assertEqual(self.request(path, headers={'Host':'attacker.test','Origin':origin})[0],403)
            self.assertEqual(self.request('/api/search', {'query':PII}, headers={'Origin':origin})[0],403)
            self.assertEqual(self.request('/api/downloads/'+'a'*32+'/file', headers={'Origin':origin})[0],403)

    def test_browser_same_origin_survives_local_header_rewriting(self):
        for origin in ('null', 'http://127.0.0.1:53935', 'chrome-extension://test'):
            headers = {'Origin': origin, 'Sec-Fetch-Site': 'same-origin'}
            with self.subTest(origin=origin), patch.object(search, 'probe', side_effect=lambda file, *_: {**file, 'status': 'missing'}):
                status, _, body = self.request('/api/search', {'query': PII, 'maximum': 1, 'extensions': ['pdf']}, headers)
                self.assertEqual(status, 200)
                self.assertIn(b'event: done\n', body)
            self.assertEqual(self.request('/api/downloads', {'pii': 'invalid'}, headers)[0], 400)
            self.assertEqual(self.request('/api/downloads/'+'a'*32+'/events', headers=headers)[0], 404)
            self.assertEqual(self.request('/api/downloads/'+'a'*32+'/file', headers=headers)[0], 404)
            self.assertEqual(self.request('/api/search', {'query': PII}, {**headers, 'X-Local-Request': ''})[0], 403)
        for site in ('cross-site', 'same-site'):
            headers = {'Origin': f'http://127.0.0.1:{self.port}', 'Sec-Fetch-Site': site}
            self.assertEqual(self.request('/api/search', {'query': PII}, headers)[0], 403)
            self.assertEqual(self.request('/api/downloads/'+'a'*32+'/file', headers=headers)[0], 403)

    def test_default_formats_can_finish_search_including_mp4(self):
        with patch.object(search, 'probe', side_effect=lambda file, *_: {**file, 'status': 'missing'}):
            status, _, body = self.request('/api/search', {'query': PII})
        self.assertEqual(status, 200)
        self.assertIn(b'.mp4', body)
        self.assertIn(b'event: done\n', body)
        self.assertNotIn(b'event: error\n', body)

    def test_search_is_sse_and_failure_cannot_reuse_previous_identity(self):
        with patch.object(search, 'probe', side_effect=lambda file, *_: {**file, 'status': 'found', 'size': 10}):
            status, headers, body = self.request('/api/search', {'query': PII, 'maximum': 1, 'extensions': ['pdf']})
        self.assertEqual(status, 200)
        self.assertIn('text/event-stream', headers['Content-Type'])
        self.assertIn(b'event: identity\n', body); self.assertIn(b'event: done\n', body)
        with patch.object(search, 'resolve_pii', return_value={'pii': '', 'status': 'not_found', 'message': 'not found'}):
            second = self.request('/api/search', {'query': 'unknown full article title'})[2]
        self.assertNotIn(PII.encode(), second)
        self.assertNotIn(b'event: batch', second)

    def test_local_download_progress_file_and_browser_resume(self):
        payload = b'%PDF-content for browser save'
        with patch.object(downloads, 'open_attachment', side_effect=lambda *a, **kw: Response(payload, headers={'Content-Length': str(len(payload))})):
            status, _, body = self.request('/api/downloads', {'pii': PII, 'files': ['1.pdf']})
            self.assertEqual(status, 202)
            task_id = json.loads(body)['id']; task = self.server.downloads.get(task_id)
            for _ in range(100):
                if task.status in downloads.TERMINAL: break
                time.sleep(.01)
            self.assertEqual(task.status, 'complete')
            events = self.request(f'/api/downloads/{task_id}/events')[2]
            self.assertIn(b'"status": "complete"', events)
            status, headers, data = self.request(f'/api/downloads/{task_id}/file')
            self.assertEqual(status, 200); self.assertEqual(data, payload)
            self.assertIn('attachment;', headers['Content-Disposition'])
            resumed = self.request(f'/api/downloads/{task_id}/file', headers={'Range': 'bytes=5-9'})
            self.assertEqual(resumed[0], 206); self.assertEqual(resumed[2], payload[5:10])
            self.assertEqual(self.request(f'/api/downloads/{task_id}/file', headers={'Range': 'bytes=9999-'})[0], 416)


if __name__ == '__main__':
    unittest.main()
