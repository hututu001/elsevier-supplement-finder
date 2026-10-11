"""Public DOI/title -> PII resolution, extracted from the original KYT/SaaS workflow.

Only public bibliographic endpoints are used. No account credentials or attachment
downloads enter this module. A failed source does not cancel the remaining sources.
"""
from __future__ import annotations

import html
import json
import re
import time
import unicodedata
import urllib.error
import urllib.request
from html.parser import HTMLParser
from urllib.parse import quote, unquote, urlencode, urljoin, urlparse

PII_RE = re.compile(r"(?<![A-Za-z0-9])(S[0-9]{15,20}X?)(?![A-Za-z0-9])", re.I)
FORMATTED_PII = re.compile(r"(?<![A-Za-z0-9])(S\d{4}-\d{4}\(\d{2}\)\d{5}-[\dX])(?![A-Za-z0-9])", re.I)
DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"'<>]+", re.I)
PUBLISHERS = {"www.sciencedirect.com", "sciencedirect.com", "linkinghub.elsevier.com", "api.elsevier.com"}
HOSTS = PUBLISHERS | {"doi.org", "dx.doi.org", "api.crossref.org"}
LIMIT = 512000


def extract_pii(value: str) -> str:
    value = str(value or "")
    for _ in range(3):
        if found := PII_RE.search(value):
            return found.group(1).upper()
        if found := FORMATTED_PII.search(value):
            return re.sub(r"[-()]", "", found.group(1)).upper()
        parsed = urlparse(value)
        if parsed.hostname in PUBLISHERS:
            found = re.search(r"/(?:pii|retrieve/pii|article/(?:abs/)?pii)/(\d{15,20}X?)(?![A-Za-z0-9])", parsed.path, re.I)
            if found:
                return "S" + found.group(1).upper()
        value = unquote(value)
    return ""


def normalize_doi(value: str) -> str:
    value = html.unescape(unquote(value or "")).translate(str.maketrans({"（": "(", "）": ")"})).replace("\\/", "/")
    match = DOI_RE.search(value)
    if not match:
        return ""
    value = match.group().split("?", 1)[0].split("#", 1)[0].rstrip(".,;，。；、]】}>")
    while value.endswith(")") and value.count(")") > value.count("("):
        value = value[:-1]
    return value.lower()


def clean_title(value: str) -> str:
    value = html.unescape(re.sub(r"<[^>]+>", " ", value or ""))
    while tag := re.match(r"^\s*[\[【]([^\]】]+)[\]】]\s*", value):
        if not re.search(r"求助|应助|补充材料|补充数据|高分|supplementary", tag.group(1), re.I):
            break
        value = value[tag.end():]
    return " ".join(unicodedata.normalize("NFKC", value).split())


def normalized_title(value: str) -> str:
    return " ".join(re.findall(r"[^\W_]+", clean_title(value).casefold()))


def title_matches(left: str, right: str) -> bool:
    a, b = normalized_title(left), normalized_title(right)
    return bool(a and b and a.replace(" ", "") == b.replace(" ", ""))


def article_url(pii: str) -> str:
    return "https://www.sciencedirect.com/science/article/pii/" + pii


def official_url(value: str) -> str:
    parsed = urlparse(value)
    try:
        valid = parsed.scheme in {"http", "https"} and parsed.hostname in HOSTS and parsed.port in (None, 443, 80) and not parsed.username and not parsed.password
    except ValueError:
        return ""
    return parsed._replace(scheme="https", netloc=parsed.hostname, fragment="").geturl() if valid else ""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_):
        return None


def fetch_public(url: str, timeout: float) -> tuple[int, dict, bytes]:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 ResearchLiterature/2.0", "Accept": "application/json,text/html;q=0.9"})
    try:
        response = urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.code, dict(response.headers), response.read(LIMIT)


class ArticleMetadata(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.identities = []
        self.dois = []
        self.visible = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"script", "style", "noscript", "template"}:
            self.hidden += 1
        key = (attrs.get("name") or attrs.get("property") or "").lower()
        if tag == "meta" and key in {"citation_pii", "dc.identifier", "og:url"}:
            self.identities.append(attrs.get("content", ""))
        if tag == "meta" and key == "citation_doi":
            self.dois.append(normalize_doi(attrs.get("content", "")))
        if tag == "link" and "canonical" in attrs.get("rel", "").lower().split():
            self.identities.append(attrs.get("href", ""))

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "template"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.visible.append(data)


def crossref_piis(item: dict) -> set[str]:
    values = item.get("alternative-id", [])
    values = [values] if isinstance(values, str) else list(values or []) if isinstance(values, list) else []
    resource = item.get("resource")
    primary = resource.get("primary") if isinstance(resource, dict) else None
    values += [item.get("URL", ""), primary.get("URL", "") if isinstance(primary, dict) else ""]
    links = item.get("link") or []
    values += [link.get("URL", "") for link in links if isinstance(link, dict)]
    return {pii for value in values if (pii := extract_pii(str(value)))}


def item_titles(item: dict) -> list[str]:
    value = item.get("title") or []
    return [value] if isinstance(value, str) else [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def resolve_pii(url="", doi="", title="", keyword="", *, fetch=None, progress=None, budget=45) -> dict:
    fetch = fetch or fetch_public
    deadline = time.monotonic() + budget
    doi = normalize_doi(doi) or normalize_doi(keyword) or normalize_doi(url)
    title = clean_title(title)
    if not title and keyword and not normalize_doi(keyword) and not extract_pii(keyword) and not urlparse(keyword).scheme and not keyword.isdigit():
        title = clean_title(keyword)
    trace, errors, attempted = [], [], set()

    def stage(source, message):
        event = {"source": source, "message": message}
        trace.append(event)
        if progress:
            progress({"type": "stage", **event})

    def result(pii="", source="", status="not_found", matched=None):
        matched = matched or {}
        return {"pii": pii, "source": source, "status": "resolved" if pii else status,
                "doi": normalize_doi(matched.get("DOI", "")) or doi,
                "title": clean_title((item_titles(matched) or [title])[0]),
                "url": article_url(pii) if pii else url,
                "stages": trace, "errors": errors, "retryable": not pii and any(e["retryable"] for e in errors),
                "message": "已解析 PII，可以检查补充材料" if pii else ("标题对应多个文献，需补充 DOI 或原文链接" if status == "ambiguous" else "暂未解析到 PII；可使用本机网络重试，或补充 DOI、原文链接、PII")}

    def get(target, source, retry=False):
        target = official_url(target)
        if not target or target in attempted:
            return None
        attempted.add(target)
        for attempt in range(2 if retry else 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                errors.append({"source": source, "message": "本次解析已到等待上限", "retryable": True})
                return None
            try:
                code, headers, data = fetch(target, min(8, remaining))
                if code >= 400:
                    transient = code in {403, 408, 429} or code >= 500
                    errors.append({"source": source, "message": f"HTTP {code}", "retryable": transient})
                    if retry and transient and attempt == 0 and code != 403:
                        continue
                    return None
                return code, {k.lower(): v for k, v in headers.items()}, data
            except (OSError, ValueError, TimeoutError):
                errors.append({"source": source, "message": "网络请求暂未完成", "retryable": True})
        return None

    def work(target, source):
        answer = get(target, source, retry=True)
        if not answer:
            return None
        try:
            message = json.loads(answer[2]).get("message")
            return message if isinstance(message, dict) else None
        except (ValueError, AttributeError):
            errors.append({"source": source, "message": "书目信息响应无效", "retryable": True})
            return None

    def publisher(target, current_doi):
        for _ in range(6):
            target = official_url(target)
            if not target:
                return ""
            if pii := extract_pii(target):
                return pii
            answer = get(target, "publisher")
            if not answer:
                return ""
            code, headers, data = answer
            if 300 <= code < 400:
                target = urljoin(target, headers.get("location", ""))
                continue
            page = ArticleMetadata()
            page.feed(data.decode("utf-8", errors="replace"))
            visible = " ".join(page.visible).lower()
            if re.search(r"article locator error|article not recognized|page not found|404\s*(?:not found|error)|access denied|verify you are human", visible):
                errors.append({"source": "publisher", "message": "出版社返回错误或验证页面", "retryable": "access denied" in visible or "verify" in visible})
                return ""
            if current_doi and page.dois and current_doi not in page.dois:
                errors.append({"source": "publisher", "message": "页面 DOI 与查询文献不一致", "retryable": False})
                return ""
            identities = {pii for value in page.identities if (pii := extract_pii(value))}
            return next(iter(identities)) if len(identities) == 1 else ""
        return ""

    for value in (url, keyword):
        if pii := extract_pii(value):
            stage("url", "输入中已包含 PII")
            return result(pii, "url")

    matched = None
    if doi:
        stage("crossref_doi", "正在按 DOI 获取书目信息")
        matched = work("https://api.crossref.org/works/" + quote(doi, safe=""), "crossref_doi")
        if matched:
            piis = crossref_piis(matched)
            if len(piis) == 1:
                return result(next(iter(piis)), "crossref_doi", matched=matched)
            if len(piis) > 1:
                return result(status="ambiguous")

    targets = [url] if official_url(url) and "api.crossref.org" not in url else []
    if doi:
        targets += ["https://doi.org/" + quote(doi, safe="/()"), "https://linkinghub.elsevier.com/retrieve/doi/" + quote(doi, safe="/()")]
    if targets:
        stage("publisher", "正在检查原文跳转与出版社文献标识")
        for target in targets:
            if pii := publisher(target, doi):
                return result(pii, "publisher", matched=matched)

    normalized = normalized_title(title)
    if len(normalized) >= 12 and len(normalized.split()) >= 3:
        stage("crossref_title", "正在按标题匹配；仅接受文字一致的文献")
        message = work("https://api.crossref.org/works?" + urlencode({"query.bibliographic": title, "rows": 5, "select": "DOI,title,link,resource,alternative-id"}), "crossref_title")
        if message:
            items = [item for item in message.get("items", []) if isinstance(item, dict)]
            exact = [item for item in items if any(normalized_title(t) == normalized for t in item_titles(item))]
            matches = exact or [item for item in items if any(title_matches(title, t) for t in item_titles(item))]
            identities = {pii for item in matches for pii in crossref_piis(item)}
            if len(identities) > 1:
                return result(status="ambiguous")
            if len(identities) == 1:
                pii = next(iter(identities))
                item = next(item for item in matches if pii in crossref_piis(item))
                return result(pii, "crossref_title", matched=item)
            dois = {normalize_doi(item.get("DOI", "")) for item in matches} - {""}
            if len(dois) > 1:
                return result(status="ambiguous")
            if matches and dois:
                item = matches[0]
                matched_doi = next(iter(dois))
                stage("publisher_title", "已匹配标题，正在检查对应 DOI 的原文跳转")
                for target in ["https://doi.org/" + quote(matched_doi, safe="/()"), "https://linkinghub.elsevier.com/retrieve/doi/" + quote(matched_doi, safe="/()")]:
                    if pii := publisher(target, matched_doi):
                        return result(pii, "publisher_title", matched=item)
            if not matches:
                errors.append({"source": "crossref_title", "message": "没有文字一致的标题匹配", "retryable": False})
    else:
        errors.append({"source": "input", "message": "标题信息不足，请补充完整标题、DOI 或原文链接", "retryable": False})
    return result(status="unavailable" if any(e["retryable"] for e in errors) else "not_found")
