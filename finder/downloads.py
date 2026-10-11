"""Downloads and ZIPs are assembled on the user's computer, never on the SaaS."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import re
import shutil
import tempfile
import threading
import time
import uuid
import zipfile

from .network import MAX_FILE_BYTES, attachment, file_response, open_attachment, response_size

CHUNK = 256 * 1024
RANGE_THRESHOLD = 8 * 1024**2
MAX_TASK_BYTES = 8 * 1024**3
TASK_TTL = 3600
TERMINAL = {'complete', 'error', 'cancelled'}


class RangeUnavailable(Exception):
    pass


class Cancelled(Exception):
    pass


class Task:
    def __init__(self, pii, files):
        self.id, self.pii, self.files = uuid.uuid4().hex, pii, files
        self.directory = Path(tempfile.mkdtemp(prefix='elsevier-supplements-'))
        self.cancel = threading.Event()
        self.lock = threading.Lock()
        self.updated = time.monotonic()
        self.rows = {file['key']: {'filename': file['filename'], 'bytes': 0, 'total': 0} for file in files}
        self.counters = {}
        self.status, self.error, self.completed, self.path = 'queued', '', 0, None
        self.serving = 0
        self.started = time.monotonic()

    def snapshot(self):
        with self.lock:
            rows = [dict(row) for row in self.rows.values()]
            received = sum(row['bytes'] for row in rows)
            return {'id': self.id, 'status': self.status, 'error': self.error, 'completed': self.completed,
                    'count': len(rows), 'bytes': received, 'total': sum(row['total'] for row in rows),
                    'speed': int(received / max(1, time.monotonic() - self.started)), 'rows': rows,
                    'url': f'/api/downloads/{self.id}/file' if self.path and self.status == 'complete' else ''}

    def state(self, status, error=''):
        with self.lock:
            self.status, self.error, self.updated = status, error, time.monotonic()

    def check(self):
        if self.cancel.is_set():
            raise Cancelled()

    def total(self, key, value):
        if value > MAX_FILE_BYTES:
            raise ValueError('单个材料超过 2 GiB，请通过文件名打开出版社链接')
        with self.lock:
            self.rows[key]['total'] = value
            if sum(row['total'] for row in self.rows.values()) > MAX_TASK_BYTES:
                raise ValueError('本次材料合计超过 8 GiB，请分批下载')

    def progress(self, key, part, value):
        with self.lock:
            self.counters[key, part] = value
            self.rows[key]['bytes'] = sum(count for (name, _), count in self.counters.items() if name == key)
            if sum(row['bytes'] for row in self.rows.values()) > MAX_TASK_BYTES:
                raise ValueError('本次材料超过 8 GiB，请分批下载')

    def reset(self, key):
        with self.lock:
            self.counters = {pair: count for pair, count in self.counters.items() if pair[0] != key}
            self.rows[key]['bytes'] = 0


def copy_response(response, destination, task, key, part=0, expected=0, inspect_prefix=True):
    received = 0
    with destination.open('wb') as output:
        while True:
            task.check()
            chunk = response.read(CHUNK)
            if not chunk:
                break
            if received == 0 and not file_response(response.headers, chunk if inspect_prefix else b'file-segment'):
                raise ValueError('出版社返回了网页，未下载为材料')
            received += len(chunk)
            if received > MAX_FILE_BYTES or expected and received > expected:
                raise ValueError('文件长度与出版社响应不一致')
            output.write(chunk)
            task.progress(key, part, received)
    if not received or expected and received != expected:
        raise ValueError('文件传输不完整，请重试')
    return received


def download_file(file, task):
    task.check()
    key, destination = file['key'], task.directory / file['filename']
    with open_attachment(file['url'], headers={'Range': 'bytes=0-0'}) as first:
        size = response_size(first)
        task.total(key, size)
        if shutil.disk_usage(task.directory).free < max(size * 2, 256 * 1024**2):
            raise ValueError('本机临时目录空间不足，请清理后重试')
        if first.status == 200:
            actual = copy_response(first, destination, task, key, expected=size)
            task.total(key, actual)
            return destination
        if first.status != 206 or not re.fullmatch(r'bytes 0-0/\d+', first.headers.get('Content-Range', '')):
            raise ValueError('出版社文件响应未确认')
        if not file_response(first.headers, first.read(1)):
            raise ValueError('出版社返回了网页，未下载为材料')
        etag = first.headers.get('ETag', '')
        validator = etag if etag and not etag.startswith('W/') else first.headers.get('Last-Modified', '')
    # If-Range guards against combining different versions. Without a validator use one stream.
    if size >= RANGE_THRESHOLD and validator:
        count = 4
        ranges = [(index * size // count, (index + 1) * size // count - 1) for index in range(count)]
        def segment(index, start, end):
            path = task.directory / (file['filename'] + f'.part{index}')
            for attempt in range(3):
                task.check()
                try:
                    with open_attachment(file['url'], headers={'Range': f'bytes={start}-{end}', 'If-Range': validator}) as response:
                        if response.status != 206 or response.headers.get('Content-Range') != f'bytes {start}-{end}/{size}':
                            raise RangeUnavailable()
                        copy_response(response, path, task, key, index, end - start + 1, inspect_prefix=index == 0)
                    return path
                except OSError:
                    if attempt == 2:
                        raise
                    task.progress(key, index, 0)
                    if task.cancel.wait(0.5 * (attempt + 1)):
                        raise Cancelled()
        try:
            with ThreadPoolExecutor(max_workers=count, thread_name_prefix='download-range') as pool:
                futures = [pool.submit(segment, index, *bounds) for index, bounds in enumerate(ranges)]
                parts = [future.result() for future in futures]
            task.check()
            with destination.open('wb') as output:
                for path in parts:
                    with path.open('rb') as source:
                        while chunk := source.read(CHUNK):
                            task.check()
                            output.write(chunk)
                    path.unlink()
            return destination
        except RangeUnavailable:
            task.reset(key)
            for path in task.directory.glob(file['filename'] + '.part*'):
                path.unlink(missing_ok=True)
    with open_attachment(file['url']) as response:
        if response.status != 200:
            raise ValueError('出版社没有返回完整文件')
        task.total(key, response_size(response))
        actual = copy_response(response, destination, task, key, expected=response_size(response))
        task.total(key, actual)
    return destination


def run_task(task):
    try:
        task.state('downloading')
        with ThreadPoolExecutor(max_workers=3, thread_name_prefix='download-file') as pool:
            futures = {pool.submit(download_file, file, task): file for file in task.files}
            try:
                for future in as_completed(futures):
                    future.result()
                    with task.lock:
                        task.completed += 1
            except BaseException:
                task.cancel.set()
                raise
        task.check()
        if len(task.files) == 1:
            task.path = task.directory / task.files[0]['filename']
        else:
            task.state('packing')
            task.path = task.directory / f'{task.pii}-supplements.zip'
            with zipfile.ZipFile(task.path, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
                for file in task.files:
                    task.check()
                    path = task.directory / file['filename']
                    with path.open('rb') as source, archive.open(file['filename'], 'w', force_zip64=True) as output:
                        while chunk := source.read(CHUNK):
                            task.check()
                            output.write(chunk)
            for file in task.files:
                (task.directory / file['filename']).unlink()
        task.state('complete')
    except Cancelled:
        task.state('cancelled')
        shutil.rmtree(task.directory, ignore_errors=True)
    except Exception as error:
        task.state('error', str(error) if isinstance(error, ValueError) else '材料传输未完成，请重试或打开出版社链接')
        shutil.rmtree(task.directory, ignore_errors=True)


class Downloads:
    def __init__(self):
        self.tasks = {}
        self.lock = threading.Lock()
        self.closed = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='download-task')
        threading.Thread(target=self.reap, daemon=True, name='temporary-file-cleanup').start()

    def create(self, body):
        if not isinstance(body, dict) or not isinstance(body.get('files'), list) or not 1 <= len(body['files']) <= 100:
            raise ValueError('一次可下载 1–100 份材料')
        if any(not isinstance(key, str) for key in body['files']):
            raise ValueError('附件列表无效')
        files = [attachment(body.get('pii', ''), key) for key in dict.fromkeys(body['files'])]
        if len(files) != len(body['files']) or len({file['key'] for file in files}) != len(files):
            raise ValueError('附件列表重复或无效')
        with self.lock:
            if sum(task.status not in TERMINAL for task in self.tasks.values()) >= 2:
                raise ValueError('已有两个下载任务，请完成后再试')
            if len(self.tasks) >= 32:
                raise ValueError('临时下载任务已满，请稍后再试或重启程序清理')
            task = Task(body['pii'], files)
            self.tasks[task.id] = task
            self.pool.submit(run_task, task)
        return task

    def get(self, identity):
        with self.lock:
            return self.tasks.get(identity)

    def reap(self):
        while not self.closed.wait(30):
            with self.lock:
                stale = [key for key, task in self.tasks.items() if task.status in TERMINAL and not task.serving and time.monotonic() - task.updated > TASK_TTL]
                for key in stale:
                    shutil.rmtree(self.tasks.pop(key).directory, ignore_errors=True)

    def close(self):
        self.closed.set()
        with self.lock:
            tasks = list(self.tasks.values())
            for task in tasks:
                task.cancel.set()
        self.pool.shutdown(wait=True, cancel_futures=True)
        for task in tasks:
            shutil.rmtree(task.directory, ignore_errors=True)
