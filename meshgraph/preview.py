"""Link previews for the chat: image detection and page metadata.

A chat message can carry short-lived image-exchange links (meshpic.org,
ibb.co and friends) whose pages wrap the real picture in ``og:image``; those
must render as pictures, while ordinary links get a title/description card.
The module fetches a URL server-side (the browser cannot read cross-origin
HTML), sniffs ``Content-Type`` for direct images, parses Open Graph tags for
pages and caches the outcome — links in mesh traffic are repeated often and
their hosts are usually slow.
"""

from __future__ import annotations

import contextlib
import ipaddress
import re
import socket
import threading
import time
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

# (connect, read) — meshgateways sit behind slow uplinks, keep both tight.
FETCH_TIMEOUT = (4.0, 8.0)
MAX_REDIRECTS = 5
MAX_HTML_BYTES = 512 * 1024
MAX_URL_CHARS = 2048
USER_AGENT = "Mozilla/5.0 (compatible; meshgraph/0.1; link preview)"

# Bare files served without HTML: detect by path alone, no request needed.
IMAGE_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp", ".svg",
)

# Quick image-exchange hosts: their pages are wrappers around ``og:image``.
IMAGE_PAGE_HOSTS = frozenset({
    "meshpic.org", "www.meshpic.org",
    "junkdata.ru", "www.junkdata.ru",
    "ibb.co", "www.ibb.co", "i.ibb.co", "imgbb.com", "www.imgbb.com",
    "imgur.com", "www.imgur.com", "i.imgur.com",
    "tmpfiles.org", "www.tmpfiles.org",
    "catbox.moe", "files.catbox.moe", "litterbox.catbox.moe",
    "0x0.st", "file.garden",
})

_MAX_TITLE_CHARS = 200
_MAX_DESC_CHARS = 400
_META_KEYS = frozenset({
    "og:title", "og:description", "og:image", "og:site_name",
    "twitter:title", "twitter:description", "twitter:image",
})

# url → (expires_at, payload); insertion order doubles as LRU.
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_LOCK = threading.Lock()
CACHE_TTL_OK = 24 * 3600
CACHE_TTL_ERROR = 15 * 60
CACHE_MAX_ENTRIES = 1000


def looks_like_image(url: str) -> bool:
    """True when the path of ``url`` ends with a known image extension."""
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in IMAGE_EXTENSIONS)


def validate_url(url: str) -> str:
    """Return the trimmed URL or raise ``ValueError`` for anything unfetchable."""
    url = (url or "").strip()
    if not url:
        raise ValueError("Link is empty.")
    if len(url) > MAX_URL_CHARS:
        raise ValueError("Link is too long.")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Only http/https links can be previewed.")
    if not parsed.netloc:
        raise ValueError("Link has no host.")
    return url


def _host_reachable(hostname: str) -> bool:
    """Refuse loopback/link-local targets: a preview must not probe ourselves.

    Private LAN addresses stay allowed — previewing a home service on
    192.168.x.x is a legitimate use of a self-hosted graph.
    """
    try:
        infos = socket.getaddrinfo(hostname, None)
    except OSError:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(str(info[4][0]).split("%")[0])
        except ValueError:
            return False
        if ip.is_loopback or ip.is_link_local:
            return False
    return True


class _MetaParser(HTMLParser):
    """Pull ``<title>`` and Open Graph/Twitter meta tags out of a page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.meta: dict[str, str] = {}
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title" and not self.title and not self._in_title:
            self._in_title = True
            return
        if tag != "meta":
            return
        values = {key: (value or "") for key, value in attrs}
        key = (values.get("property") or values.get("name") or "").strip().lower()
        if key in _META_KEYS and key not in self.meta:
            self.meta[key] = values.get("content", "").strip()

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title and not self.title:
            self.title += data


def _clean(value: str, limit: int) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    return value[:limit]


def _error(reason: str) -> dict[str, Any]:
    return {"kind": "error", "reason": reason}


def _open(url: str) -> Any:
    """GET ``url`` as a context manager: the session must outlive the body.

    ``stream=True`` means the HTML is read inside the ``with`` block, and only
    then ``session.close()`` runs — closing it earlier would kill the socket
    under a half-read response. ``max_redirects`` lives on the session, which
    is why a fresh one is created per fetch.
    """
    @contextlib.contextmanager
    def opener():
        session = requests.Session()
        session.max_redirects = MAX_REDIRECTS
        try:
            yield session.get(
                url,
                stream=True,
                timeout=FETCH_TIMEOUT,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,image/*,*/*;q=0.8",
                },
            )
        finally:
            session.close()

    return opener()


def _read_html(response: requests.Response) -> str:
    buffer = b""
    for chunk in response.iter_content(65536):
        buffer += chunk
        if len(buffer) >= MAX_HTML_BYTES:
            break
    encoding = response.encoding or "utf-8"
    try:
        text = buffer.decode(encoding, "replace")
    except LookupError:
        text = buffer.decode("utf-8", "replace")
    return text[:MAX_HTML_BYTES]


def _upgrade_to_https(page_url: str, image_url: str) -> str:
    """A page served over https must not embed a mixed-content http image."""
    if page_url.startswith("https://") and image_url.startswith("http://"):
        page, image = urlparse(page_url), urlparse(image_url)
        if page.netloc == image.netloc:
            return "https://" + image_url[len("http://"):]
    return image_url


def _parse_page(page_url: str, html: str) -> dict[str, Any]:
    parser = _MetaParser()
    try:
        parser.feed(html)
    except Exception:  # malformed HTML must not fail the whole preview
        pass
    meta = parser.meta
    site = _clean(meta.get("og:site_name", ""), 60) or urlparse(page_url).netloc
    image = meta.get("og:image") or meta.get("twitter:image") or ""
    if image:
        image = urljoin(page_url, image)
    return {
        "title": _clean(meta.get("og:title") or meta.get("twitter:title")
                        or parser.title, _MAX_TITLE_CHARS),
        "description": _clean(meta.get("og:description")
                              or meta.get("twitter:description"), _MAX_DESC_CHARS),
        "image": image,
        "site": site,
    }


def _fetch_once(url: str) -> dict[str, Any]:
    with _open(url) as response:
        if response.status_code >= 400:
            return _error(f"http {response.status_code}")
        final_url = response.url or url
        content_type = (response.headers.get("Content-Type") or "")
        content_type = content_type.split(";")[0].strip().lower()

        if content_type.startswith("image/") or looks_like_image(final_url):
            return {"kind": "image", "url": final_url, "page": url}

        if content_type in ("text/html", "application/xhtml+xml", ""):
            page = _parse_page(final_url, _read_html(response))
            host = urlparse(final_url).hostname or ""
            image = page["image"]
            if image and host in IMAGE_PAGE_HOSTS:
                return {
                    "kind": "image",
                    "url": _upgrade_to_https(final_url, image),
                    "page": final_url,
                }
            return {"kind": "page", "url": final_url, **page}

        # Anything else (video, pdf, json): no metadata worth showing.
        return {
            "kind": "page", "url": final_url,
            "title": "", "description": "", "image": "",
            "site": urlparse(final_url).netloc,
        }


def _fetch(url: str) -> dict[str, Any]:
    if looks_like_image(url):
        # No request: the <img> tag itself verifies the link and the chat
        # falls back to a plain chip when the file is gone.
        return {"kind": "image", "url": url, "page": url}

    parsed = urlparse(url)
    if not _host_reachable(parsed.hostname or ""):
        return _error("unreachable host")

    # Connection-level failures get one retry: image hosts behind slow edges
    # often drop the very first attempt. HTTP errors return a dict (no
    # exception), so dead links are not fetched twice.
    for _attempt in range(2):
        try:
            return _fetch_once(url)
        except requests.RequestException:
            continue
    return _error("fetch failed")


def get_preview(url: str) -> dict[str, Any]:
    """Cached preview for ``url`` (validation happens in the web layer too)."""
    now = time.monotonic()
    with _CACHE_LOCK:
        entry = _CACHE.get(url)
        if entry and entry[0] > now:
            return entry[1]

    try:
        result = _fetch(url)
    except Exception:  # a broken preview must never take the chat down
        result = _error("preview failed")

    ttl = CACHE_TTL_ERROR if result["kind"] == "error" else CACHE_TTL_OK
    with _CACHE_LOCK:
        if len(_CACHE) >= CACHE_MAX_ENTRIES:
            for key, (expiry, _) in list(_CACHE.items()):
                if expiry <= now or len(_CACHE) >= CACHE_MAX_ENTRIES:
                    _CACHE.pop(key, None)
                if len(_CACHE) < CACHE_MAX_ENTRIES // 2:
                    break
        _CACHE[url] = (time.monotonic() + ttl, result)
    return result


def reset_cache() -> None:
    """Drop every cached preview (tests and manual refreshes)."""
    with _CACHE_LOCK:
        _CACHE.clear()
