"""Small Range probes, bounded concurrent work, session-only public-result cache."""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from collections import OrderedDict
from itertools import count
import threading
import time
import urllib.error

from .identity import resolve_pii
from .network import EXTENSIONS, attachment, file_response, open_attachment, response_size

CACHE = OrderedDict()
CACHE_LOCK = threading.Lock()
GENERATIONS = count()
NETWORK_SLOTS = threading.BoundedSemaphore(88)
SEARCH_SLOTS = threading.BoundedSemaphore(3)


def validate_search(body):
    if not isinstance(body, dict):
        raise ValueError('查询参数无效')
    keyword = body.get('query', '')
    maximum, concurrency = body.get('maximum', 11), body.get('concurrency', 32)
    extensions = body.get('extensions', list(EXTENSIONS[:15]))
    if not isinstance(keyword, str) or not 1 <= len(keyword.strip()) <= 2000:
        raise ValueError('请输入文章标题、DOI、ScienceDirect 链接或 PII')
    if type(maximum) is not int or not 1 <= maximum <= 100:
        raise ValueError('附件编号应为 1–100')
    if type(concurrency) is not int or not 1 <= concurrency <= 88:
        raise ValueError('检查并发应为 1–88')
    if not isinstance(extensions, list) or not 1 <= len(extensions) <= len(EXTENSIONS) or any(not isinstance(ext, str) or ext not in EXTENSIONS for ext in extensions):
        raise ValueError('请选择有效的文件类型')
    if type(body.get('refresh', False)) is not bool:
        raise ValueError('重新检查参数无效')
    return keyword.strip(), maximum, list(dict.fromkeys(extensions)), concurrency


def probe(file, refresh=False):
    key = file['filename']
    with CACHE_LOCK:
        started = next(GENERATIONS)
        cached = CACHE.get(key)
        if not refresh and cached and cached[0] > time.monotonic():
            CACHE.move_to_end(key)
            return {**cached[1], 'cached': True}
        CACHE.pop(key, None)
    result = {**file, 'status': 'uncertain', 'size': 0, 'cached': False}
    try:
        with NETWORK_SLOTS, open_attachment(file['url'], headers={'Range': 'bytes=0-1023'}, timeout=6) as response:
            prefix = response.read(1024)
            if response.status in (200, 206) and file_response(response.headers, prefix):
                result.update(status='found', size=response_size(response))
            else:
                result['reason'] = '返回了网页或空响应，尚未确认文件'
    except urllib.error.HTTPError as error:
        result.update(status='missing' if error.code in (404, 410) else 'uncertain', reason=f'HTTP {error.code}')
        error.close()
    except (OSError, ValueError):
        result['reason'] = '网络未确认，可重试或打开候选链接'
    if result['status'] != 'uncertain':
        with CACHE_LOCK:
            if key not in CACHE or CACHE[key][2] <= started:
                CACHE[key] = (time.monotonic() + 86400, result, started)
            while len(CACHE) > 8192:
                CACHE.popitem(last=False)
    return result


def search(body, send):
    keyword, maximum, extensions, concurrency = validate_search(body)
    identity = resolve_pii(keyword=keyword, url=keyword if keyword.startswith(('http://', 'https://')) else '',
                           progress=lambda stage: send('stage', stage))
    send('identity', identity)
    if not identity['pii']:
        send('done', {'checked': 0, 'total': 0})
        return
    files = iter(attachment(identity['pii'], f'{number}.{ext}') for number in range(1, maximum + 1) for ext in extensions)
    total, checked = maximum * len(extensions), 0
    send('progress', {'checked': 0, 'total': total})
    executor = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix='probe')
    pending = set()
    try:
        for file in files:
            pending.add(executor.submit(probe, file, body.get('refresh', False)))
            if len(pending) >= concurrency:
                break
        while pending:
            complete, pending = wait(pending, timeout=1, return_when=FIRST_COMPLETED)
            if not complete:
                send('heartbeat', {})
                continue
            rows = [future.result() for future in complete]
            checked += len(rows)
            send('batch', {'results': rows, 'checked': checked, 'total': total})
            for _ in complete:
                file = next(files, None)
                if file is not None:
                    pending.add(executor.submit(probe, file, body.get('refresh', False)))
        send('done', {'checked': checked, 'total': total})
    finally:
        for future in pending:
            future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
