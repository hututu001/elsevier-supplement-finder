import io
import json
import hashlib
import shutil
import threading
import time
import unittest
import urllib.error
import zipfile
from unittest.mock import patch

from finder import downloads, network, search

PII = 'S0017931026010112'


class Response(io.BytesIO):
    def __init__(self, body, status=200, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = {'Content-Type': 'application/octet-stream', **(headers or {})}


class ProbeTests(unittest.TestCase):
    def setUp(self):
        search.CACHE.clear()

    def test_candidate_validation_and_search_settings(self):
        self.assertIn('-mmc11.jpeg', network.attachment(PII, '11.jpeg')['url'])
        for key in ['0.pdf', '101.pdf', '../1.pdf', '1.exe', '1.pdf?x=1', 'https://example.com']:
            with self.assertRaises(ValueError):
                network.attachment(PII, key)
        for change in [{'maximum': True}, {'concurrency': 89}, {'extensions': []}, {'extensions': [None]}, {'refresh': 'yes'}]:
            with self.assertRaises(ValueError):
                search.validate_search({'query': PII, **change})

    def test_probe_reads_only_prefix_and_cache_skips_network(self):
        file = network.attachment(PII, '1.pdf')
        response = Response(b'%PDF' + b'a' * 5000, headers={'Content-Length': '5004'})
        with patch.object(search, 'open_attachment', return_value=response) as fetch:
            first = search.probe(file)
            self.assertEqual(first['status'], 'found')
            self.assertEqual(first['size'], 5004)
            self.assertEqual(fetch.call_args.kwargs['headers']['Range'], 'bytes=0-1023')
            self.assertTrue(response.closed)
            self.assertTrue(search.probe(file)['cached'])
            self.assertEqual(fetch.call_count, 1)

    def test_only_404_and_410_mean_missing_and_force_rechecks(self):
        file = network.attachment(PII, '1.pdf')
        for code, expected in [(404, 'missing'), (410, 'missing'), (403, 'uncertain'), (429, 'uncertain'), (503, 'uncertain')]:
            with patch.object(search, 'open_attachment', side_effect=urllib.error.HTTPError(file['url'], code, 'error', {}, None)):
                self.assertEqual(search.probe(file, True)['status'], expected)
        with patch.object(search, 'open_attachment', return_value=Response(b'<html>blocked</html>')):
            self.assertEqual(search.probe(file, True)['status'], 'uncertain')
        with patch.object(search, 'open_attachment', return_value=Response(b'%PDF-new', headers={'Content-Length': '8'})):
            self.assertEqual(search.probe(file, True)['status'], 'found')

    def test_old_probe_cannot_overwrite_forced_result(self):
        file = network.attachment(PII, '1.pdf')
        ready, release = threading.Event(), threading.Event()
        def fetch(*args, **kwargs):
            if threading.current_thread().name == 'old-probe':
                ready.set(); release.wait(2)
                raise urllib.error.HTTPError(file['url'], 404, 'missing', {}, None)
            return Response(b'new', headers={'Content-Length': '3'})
        with patch.object(search, 'open_attachment', side_effect=fetch):
            thread = threading.Thread(target=lambda: search.probe(file), name='old-probe'); thread.start()
            self.assertTrue(ready.wait(2))
            self.assertEqual(search.probe(file, True)['status'], 'found')
            release.set(); thread.join(2)
            self.assertEqual(search.probe(file)['status'], 'found')

    def test_stream_emits_identity_results_and_completion_with_bounded_parallelism(self):
        events, active, peak = [], 0, 0
        lock = threading.Lock()
        def probe(file, refresh):
            nonlocal active, peak
            with lock: active += 1; peak = max(peak, active)
            time.sleep(.01)
            with lock: active -= 1
            return {**file, 'status': 'found', 'size': 10}
        with patch.object(search, 'probe', side_effect=probe):
            search.search({'query': PII, 'maximum': 3, 'extensions': ['pdf', 'xlsx'], 'concurrency': 2}, lambda name, data: events.append((name, data)))
        self.assertEqual(next(data for name, data in events if name == 'identity')['pii'], PII)
        self.assertEqual(events[-1], ('done', {'checked': 6, 'total': 6}))
        self.assertEqual(sum(len(data['results']) for name, data in events if name == 'batch'), 6)
        self.assertLessEqual(peak, 2)

    def test_untrusted_publisher_redirect_is_rejected(self):
        redirect = network.PublisherRedirect()
        for url in ['http://ars.els-cdn.com/file', 'https://evil.test/file', 'https://ars.els-cdn.com:444/file', 'https://user@ars.els-cdn.com/file']:
            with self.assertRaises(ValueError):
                redirect.redirect_request(None, None, 302, '', {}, url)


class DownloadTests(unittest.TestCase):
    def make_task(self, keys=None):
        task = downloads.Task(PII, [network.attachment(PII, key) for key in (keys or ['1.pdf'])])
        self.addCleanup(shutil.rmtree, task.directory, True)
        return task

    def test_four_range_download_matches_original_bytes(self):
        body = bytearray(bytes(range(256)) * 32769)
        # A middle segment can start with HTML-like bytes inside a valid binary file.
        boundary = len(body) // 4
        body[boundary:boundary + 6] = b'<html>'
        body = bytes(body)
        calls, lock = [], threading.Lock()
        def fetch(url, *, headers=None, **kwargs):
            with lock: calls.append(headers or {})
            value = (headers or {}).get('Range')
            if value:
                start, end = map(int, value[6:].split('-'))
                return Response(body[start:end + 1], 206, {'Content-Range': f'bytes {start}-{end}/{len(body)}', 'ETag': '"stable"'})
            return Response(body, headers={'Content-Length': str(len(body))})
        task = self.make_task()
        with patch.object(downloads, 'open_attachment', side_effect=fetch):
            result = downloads.download_file(task.files[0], task)
        self.assertEqual(hashlib.sha256(result.read_bytes()).digest(), hashlib.sha256(body).digest())
        self.assertEqual(len(calls), 5)
        self.assertTrue(all(call.get('If-Range') == '"stable"' for call in calls[1:]))
        self.assertEqual(task.snapshot()['bytes'], len(body))

    def test_ignored_range_falls_back_to_complete_stream_and_zip_is_valid(self):
        payload = b'%PDF-1.7 test attachment'
        task = self.make_task(['1.pdf', '2.docx'])
        with patch.object(downloads, 'open_attachment', side_effect=lambda *a, **k: Response(payload, headers={'Content-Length': str(len(payload))})):
            downloads.run_task(task)
        self.assertEqual(task.status, 'complete')
        with zipfile.ZipFile(task.path) as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(set(archive.namelist()), {file['filename'] for file in task.files})
            for name in archive.namelist(): self.assertEqual(archive.read(name), payload)

    def test_changed_range_validator_falls_back_without_combining_versions(self):
        payload = b'A' * (downloads.RANGE_THRESHOLD + 100)
        task = self.make_task()
        def fetch(url, *, headers=None, **kwargs):
            if (headers or {}).get('Range') == 'bytes=0-0':
                return Response(b'A', 206, {'Content-Range': f'bytes 0-0/{len(payload)}', 'ETag': '"old"'})
            return Response(payload, headers={'Content-Length': str(len(payload))})
        with patch.object(downloads, 'open_attachment', side_effect=fetch):
            destination = downloads.download_file(task.files[0], task)
        self.assertEqual(destination.read_bytes(), payload)
        self.assertEqual(task.snapshot()['bytes'], len(payload))

    def test_truncated_file_and_html_do_not_create_successful_download(self):
        for payload, headers in [(b'%PDF-short', {'Content-Length': '999'}), (b'<html>blocked</html>', {})]:
            task = self.make_task()
            with patch.object(downloads, 'open_attachment', return_value=Response(payload, headers=headers)):
                downloads.run_task(task)
            self.assertEqual(task.status, 'error')
            self.assertFalse(task.directory.exists())

    def test_cancelled_task_leaves_no_files(self):
        task = self.make_task(); task.cancel.set()
        downloads.run_task(task)
        self.assertEqual(task.status, 'cancelled')
        self.assertFalse(task.directory.exists())

    def test_download_manager_rejects_duplicates_unhashable_keys_and_arbitrary_urls(self):
        manager = downloads.Downloads(); self.addCleanup(manager.close)
        for files in [['1.pdf', '1.pdf'], ['01.pdf', '1.pdf'], [[]], ['https://evil.test/file'], []]:
            with self.assertRaises(ValueError): manager.create({'pii': PII, 'files': files})


if __name__ == '__main__':
    unittest.main()
