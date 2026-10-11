"""A loopback web application with no framework or installation step."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import re
import threading
import time
from urllib.parse import quote, urlparse

from .downloads import Downloads, TERMINAL
from .network import DEFAULT_EXTENSIONS, EXTENSIONS
from .search import SEARCH_SLOTS, search, validate_search

STATIC = Path(__file__).resolve().parent.parent / 'static'
ASSETS = {'/': ('index.html', 'text/html'), '/app.js': ('app.js', 'text/javascript'), '/style.css': ('style.css', 'text/css')}
ASSETS['/images/logo.webp'] = ('images/logo.webp', 'image/webp')
ASSETS['/images/favicon.png'] = ('images/favicon.png', 'image/png')
for name in ('logo.svg', 'supplement-backdrop.svg', 'supplement-documents.svg', 'supplement-formats.svg'):
    ASSETS['/images/' + name] = ('images/' + name, 'image/svg+xml')
POLICY = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address):
        self.downloads = None
        super().__init__(address, Handler)
        try:
            self.downloads = Downloads()
        except BaseException:
            self.server_close()
            raise

    def server_close(self):
        super().server_close()
        if self.downloads is not None:
            self.downloads.close()


class Handler(BaseHTTPRequestHandler):
    server_version = 'SupplementFinder/1.0'
    sys_version = ''

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def trusted(self, mutation=False, *, public_resource=False):
        host = self.headers.get('Host', '')
        try:
            parsed = urlparse('http://' + host)
            if parsed.hostname not in {'localhost', '127.0.0.1', '::1'} or parsed.username or parsed.password or parsed.path:
                return False
            if not public_resource:
                # Fetch Metadata is set by the browser, not page JavaScript.
                # Local network filters may rewrite Host/Origin when forwarding
                # a request; same-origin requests still belong to this page.
                site = self.headers.get('Sec-Fetch-Site')
                if site in ('cross-site', 'same-site'):
                    return False
                if site != 'same-origin' and self.headers.get('Origin') not in (None, 'http://' + host):
                    return False
        except ValueError:
            return False
        return not mutation or self.headers.get('X-Local-Request') == '1'

    def headers_for(self, status, content_type, length=None, **headers):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', POLICY)
        self.send_header('Referrer-Policy', 'no-referrer')
        if length is not None:
            self.send_header('Content-Length', str(length))
        for key, value in headers.items():
            self.send_header(key.replace('_', '-'), str(value))
        self.end_headers()

    def json(self, data, status=200):
        content = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.headers_for(status, 'application/json; charset=utf-8', len(content))
        self.wfile.write(content)

    def body(self):
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json' or self.headers.get('Transfer-Encoding'):
            raise ValueError('请使用 JSON 请求')
        length = int(self.headers.get('Content-Length', '0'))
        if not 0 < length <= 32768:
            raise ValueError('请求内容为空或过长')
        return json.loads(self.rfile.read(length))

    def begin_events(self):
        self.headers_for(200, 'text/event-stream; charset=utf-8', X_Accel_Buffering='no')

    def event(self, name, data):
        message = f'event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n'.encode('utf-8')
        self.wfile.write(message)
        self.wfile.flush()

    def do_POST(self):
        if not self.trusted(mutation=True):
            self.json({'error': '请从本地网页执行操作'}, 403)
            return
        path = urlparse(self.path).path
        try:
            body = self.body()
            if path == '/api/search':
                validate_search(body)
                if not SEARCH_SLOTS.acquire(blocking=False):
                    self.json({'error': '已有查询正在运行，请稍后再试'}, 429)
                    return
                try:
                    self.begin_events()
                    try:
                        search(body, self.event)
                    except (BrokenPipeError, ConnectionError, TimeoutError):
                        pass
                    except Exception:
                        self.event('error', {'message': '查询未完成，已找到的结果仍保留，请重试'})
                finally:
                    SEARCH_SLOTS.release()
            elif path == '/api/downloads':
                self.json(self.server.downloads.create(body).snapshot(), 202)
            elif match := re.fullmatch(r'/api/downloads/([a-f0-9]{32})/cancel', path):
                task = self.server.downloads.get(match[1])
                if not task:
                    self.json({'error': '下载任务已过期'}, 404)
                else:
                    task.cancel.set()
                    self.json({'ok': True})
            else:
                self.json({'error': '接口不存在'}, 404)
        except json.JSONDecodeError:
            self.json({'error': '查询参数无效，请检查输入和检索设置'}, 400)
        except ValueError as error:
            self.json({'error': str(error) or '查询参数无效'}, 400)
        except TypeError:
            self.json({'error': '查询参数无效，请检查输入和检索设置'}, 400)
        except (BrokenPipeError, ConnectionError, TimeoutError):
            pass
        except OSError:
            self.json({'error': '无法创建本机临时文件，请检查磁盘空间与目录权限'}, 503)

    def do_GET(self):
        path = urlparse(self.path).path
        public_resource = path in ASSETS or path in {'/api/settings', '/api/health'}
        if not self.trusted(public_resource=public_resource):
            self.json({'error': '请使用 localhost 或 127.0.0.1 打开本地网页'}, 403)
            return
        try:
            if path in ASSETS:
                filename, mime = ASSETS[path]
                data = (STATIC / filename).read_bytes()
                content_type = mime + '; charset=utf-8' if mime.startswith('text/') or mime == 'image/svg+xml' else mime
                self.headers_for(200, content_type, len(data))
                self.wfile.write(data)
            elif path == '/api/settings':
                self.json({'extensions': EXTENSIONS, 'defaultExtensions': DEFAULT_EXTENSIONS,
                           'maximum': 11, 'concurrency': 32, 'version': '1.0.0'})
            elif path == '/api/health':
                self.json({'ok': True, 'version': '1.0.0'})
            elif match := re.fullmatch(r'/api/downloads/([a-f0-9]{32})/(events|file)', path):
                task = self.server.downloads.get(match[1])
                if not task:
                    self.json({'error': '下载任务已过期，请重新下载'}, 404)
                elif match[2] == 'events':
                    self.begin_events()
                    while True:
                        snapshot = task.snapshot()
                        self.event('progress', snapshot)
                        if snapshot['status'] in TERMINAL:
                            break
                        time.sleep(0.5)
                else:
                    self.file(task)
            else:
                self.json({'error': '页面不存在'}, 404)
        except (BrokenPipeError, ConnectionError, TimeoutError):
            pass

    def file(self, task):
        with task.lock:
            if task.status != 'complete' or not task.path or not task.path.is_file():
                self.json({'error': '文件尚未完成或已过期'}, 409)
                return
            task.serving = getattr(task, 'serving', 0) + 1
            task.updated = time.monotonic()
        try:
            size = task.path.stat().st_size
            start, end = 0, size - 1
            requested = self.headers.get('Range')
            if requested:
                match = re.fullmatch(r'bytes=(\d+)-(\d*)', requested)
                if not match or int(match[1]) >= size or match[2] and int(match[2]) < int(match[1]):
                    self.headers_for(416, 'application/octet-stream', 0, Content_Range=f'bytes */{size}')
                    return
                start = int(match[1])
                end = min(end, int(match[2])) if match[2] else end
            headers = {'Accept_Ranges': 'bytes', 'Content_Disposition': f"attachment; filename*=UTF-8''{quote(task.path.name)}"}
            if requested:
                headers['Content_Range'] = f'bytes {start}-{end}/{size}'
            with task.path.open('rb') as file:
                self.headers_for(206 if requested else 200, 'application/octet-stream', end - start + 1, **headers)
                file.seek(start)
                remaining = end - start + 1
                while remaining:
                    chunk = file.read(min(256 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        finally:
            with task.lock:
                task.serving -= 1
                task.updated = time.monotonic()

    def log_message(self, format, *args):
        # POST bodies (titles/DOIs) are deliberately not logged.
        pass
