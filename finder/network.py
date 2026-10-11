"""Fixed public destinations; environment proxy support without application credentials."""
import re
import urllib.request
from urllib.parse import urlparse

EXTENSIONS = ('pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx', 'csv', 'zip', 'mp4', 'mov', 'jpg', 'tif', 'png', 'gif', 'jpeg', 'tiff')
DEFAULT_EXTENSIONS = EXTENSIONS[:15]
PII = re.compile(r'S\d{15,20}X?')
FILE_KEY = re.compile(r'(\d{1,3})\.([a-z]+)')
MAX_FILE_BYTES = 2 * 1024**3


def attachment(pii, key):
    match = FILE_KEY.fullmatch(str(key))
    if not PII.fullmatch(str(pii)) or not match or not 1 <= int(match[1]) <= 100 or match[2] not in EXTENSIONS:
        raise ValueError('附件标识无效')
    filename = f'1-s2.0-{pii}-mmc{int(match[1])}.{match[2]}'
    return {'key': f'{int(match[1])}.{match[2]}', 'filename': filename,
            'url': 'https://ars.els-cdn.com/content/image/' + filename}


class PublisherRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 2
    max_repeats = 1

    def redirect_request(self, request, fp, code, message, headers, url):
        parsed = urlparse(url)
        if parsed.scheme != 'https' or parsed.hostname != 'ars.els-cdn.com' or parsed.port not in (None, 443) or parsed.username or parsed.password:
            raise ValueError('出版社跳转地址未确认')
        return super().redirect_request(request, fp, code, message, headers, url)


def open_attachment(url, *, headers=None, timeout=20):
    parsed = urlparse(url)
    if parsed.scheme != 'https' or parsed.hostname != 'ars.els-cdn.com' or parsed.port not in (None, 443) or parsed.username or parsed.password:
        raise ValueError('仅支持已构造的出版社材料地址')
    request = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.sciencedirect.com/',
        'Accept-Encoding': 'identity', **(headers or {}),
    })
    return urllib.request.build_opener(PublisherRedirect()).open(request, timeout=timeout)


def file_response(headers, prefix):
    content_type = headers.get('Content-Type', '').lower()
    prefix = prefix.lstrip().lower()
    return bool(prefix) and not ('text/html' in content_type or 'application/json' in content_type or prefix.startswith((b'<!doctype html', b'<html', b'{"')))


def response_size(response):
    if response.status == 206:
        value = response.headers.get('Content-Range', '').rsplit('/', 1)[-1]
    else:
        value = response.headers.get('Content-Length', '')
    return int(value) if value.isdigit() else 0
