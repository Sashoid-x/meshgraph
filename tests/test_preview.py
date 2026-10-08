"""Link previews: image detection, Open Graph parsing and the cache.

The network is replaced by a fake response object: every test that goes
through ``_fetch`` patches ``_open`` (the single seam to requests) and
``_host_reachable`` so the suite never touches DNS or the wire.
"""

from __future__ import annotations

import contextlib

import pytest

from meshgraph import preview

# Captured before the fixture swaps it out, so the loopback test can restore
# the real DNS/IP check without touching the network.
REAL_HOST_REACHABLE = preview._host_reachable


class FakeResponse:
    """Duck-typed ``requests.Response`` for the streaming read in ``_fetch``."""

    def __init__(
        self,
        *,
        url: str,
        status: int = 200,
        content_type: str = "text/html; charset=utf-8",
        body: bytes = b"",
        encoding: str | None = None,
    ) -> None:
        self.status_code = status
        self.url = url
        self.headers = {"Content-Type": content_type}
        self.encoding = encoding
        self._body = body

    def iter_content(self, chunk_size: int):
        for start in range(0, len(self._body), chunk_size):
            yield self._body[start:start + chunk_size]


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """No DNS, no cache bleed between tests."""
    preview.reset_cache()
    monkeypatch.setattr(preview, "_host_reachable", lambda host: True)
    yield
    preview.reset_cache()


def patch_open(monkeypatch, response):
    """Route the fetch seam to a canned response (or an exception)."""
    if isinstance(response, Exception):
        monkeypatch.setattr(
            preview, "_open",
            lambda url: (_ for _ in ()).throw(response),
        )
    else:
        monkeypatch.setattr(
            preview, "_open", lambda url: contextlib.nullcontext(response)
        )


def forbidden(monkeypatch):
    """Make any network attempt fail the test loudly."""
    monkeypatch.setattr(
        preview, "_open",
        lambda url: (_ for _ in ()).throw(AssertionError("unexpected fetch")),
    )


# ---------------------------------------------------------------------------
# Detection without a network round trip
# ---------------------------------------------------------------------------


def test_image_extension_is_detected_without_fetching(monkeypatch):
    forbidden(monkeypatch)
    result = preview.get_preview("https://files.example.org/pic.GIF?dl=1")
    assert result == {
        "kind": "image",
        "url": "https://files.example.org/pic.GIF?dl=1",
        "page": "https://files.example.org/pic.GIF?dl=1",
    }


def test_image_extension_recognises_every_supported_format():
    for ext in ("jpg", "jpeg", "png", "gif", "webp", "avif", "bmp", "svg"):
        assert preview.looks_like_image(f"https://x.example/a.{ext}")
        assert preview.looks_like_image(f"https://x.example/a.{ext.upper()}")
    assert not preview.looks_like_image("https://x.example/a.mp4")
    assert not preview.looks_like_image("https://x.example/imaginary")


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def test_direct_image_content_type_yields_the_image(monkeypatch):
    patch_open(monkeypatch, FakeResponse(
        url="https://cdn.example.com/get?id=7",
        content_type="image/png",
    ))
    result = preview.get_preview("https://cdn.example.com/get?id=7")
    assert result["kind"] == "image"
    assert result["url"] == "https://cdn.example.com/get?id=7"


def test_redirect_to_image_file_is_recognised(monkeypatch):
    patch_open(monkeypatch, FakeResponse(
        url="https://cdn.example.com/final/pic.jpg",
        content_type="text/html",  # сервер врачит про html, адрес решает
    ))
    result = preview.get_preview("https://short.example/abc")
    assert result == {
        "kind": "image",
        "url": "https://cdn.example.com/final/pic.jpg",
        "page": "https://short.example/abc",
    }


@pytest.mark.parametrize("host", ["meshpic.org", "junkdata.ru"])
def test_image_exchange_host_is_unfurled_through_og_image(monkeypatch, host):
    html = f"""<html><head>
      <meta property="og:title" content="{host} - временная картинка">
      <meta property="og:image" content="http://{host}/image/abc">
      </head><body>page</body></html>""".encode()
    page_url = f"https://{host}/abc"
    patch_open(monkeypatch, FakeResponse(url=page_url, body=html))
    result = preview.get_preview(page_url)
    # Страница отдалась по https — og:image обязан перейти на https, иначе
    # браузер заблокирует смешанный контент.
    assert result == {
        "kind": "image",
        "url": f"https://{host}/image/abc",
        "page": page_url,
    }


def test_regular_page_returns_title_description_and_thumbnail(monkeypatch):
    html = b"""<html><head>
      <title>Old title</title>
      <meta property="og:title" content="meshgraph release">
      <meta property="og:description" content="A graph with a chat">
      <meta property="og:image" content="/shots/release.png">
      <meta property="og:site_name" content="GitHub">
      </head></html>"""
    patch_open(monkeypatch, FakeResponse(url="https://github.example/r", body=html))
    result = preview.get_preview("https://github.example/r")
    assert result["kind"] == "page"
    assert result["title"] == "meshgraph release"
    assert result["description"] == "A graph with a chat"
    assert result["image"] == "https://github.example/shots/release.png"
    assert result["site"] == "GitHub"


def test_page_without_meta_falls_back_to_title_and_host(monkeypatch):
    html = b"<html><head><title>  Simple \n page </title></head></html>"
    patch_open(monkeypatch, FakeResponse(url="https://plain.example/x", body=html))
    result = preview.get_preview("https://plain.example/x")
    assert result["kind"] == "page"
    assert result["title"] == "Simple page"
    assert result["image"] == ""
    assert result["site"] == "plain.example"


def test_non_html_content_type_is_a_bare_page_card(monkeypatch):
    patch_open(monkeypatch, FakeResponse(
        url="https://video.example/clip.mp4",
        content_type="video/mp4",
    ))
    result = preview.get_preview("https://video.example/clip.mp4")
    assert result == {
        "kind": "page", "url": "https://video.example/clip.mp4",
        "title": "", "description": "", "image": "",
        "site": "video.example",
    }


def test_http_error_status_becomes_an_error_result(monkeypatch):
    patch_open(monkeypatch, FakeResponse(
        url="https://gone.example/abc", status=404,
    ))
    result = preview.get_preview("https://gone.example/abc")
    assert result == {"kind": "error", "reason": "http 404"}


def test_network_failure_becomes_an_error_result(monkeypatch):
    import requests

    patch_open(monkeypatch, requests.ConnectionError("no route"))
    result = preview.get_preview("https://down.example/")
    assert result == {"kind": "error", "reason": "fetch failed"}


def test_loopback_targets_are_refused(monkeypatch):
    forbidden(monkeypatch)
    monkeypatch.setattr(preview, "_host_reachable", REAL_HOST_REACHABLE)
    result = preview.get_preview("http://127.0.0.1:5010/api/status")
    assert result == {"kind": "error", "reason": "unreachable host"}


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------


def test_validate_url_accepts_http_and_https():
    assert preview.validate_url("  https://a.example/x  ") == "https://a.example/x"
    assert preview.validate_url("http://a.example/") == "http://a.example/"


@pytest.mark.parametrize("bad", ["", "   ", "ftp://a.example/x", "javascript:alert(1)",
                                 "file:///etc/passwd", "https://", "//a.example"])
def test_validate_url_rejects_anything_unfetchable(bad):
    with pytest.raises(ValueError):
        preview.validate_url(bad)


def test_validate_url_rejects_oversized_links():
    with pytest.raises(ValueError):
        preview.validate_url("https://a.example/" + "x" * preview.MAX_URL_CHARS)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def test_successful_preview_is_cached(monkeypatch):
    calls = []

    def open_once(url):
        calls.append(url)
        return contextlib.nullcontext(
            FakeResponse(url=url, content_type="text/html", body=b"<title>t</title>")
        )

    monkeypatch.setattr(preview, "_open", open_once)
    first = preview.get_preview("https://a.example/page")
    second = preview.get_preview("https://a.example/page")
    assert first == second
    assert len(calls) == 1


def test_reset_cache_forces_a_refetch(monkeypatch):
    calls = []
    monkeypatch.setattr(
        preview, "_open",
        lambda url: (calls.append(url) or contextlib.nullcontext(
            FakeResponse(url=url, content_type="image/jpeg"))),
    )
    preview.get_preview("https://a.example/p")
    preview.reset_cache()
    preview.get_preview("https://a.example/p")
    assert len(calls) == 2


def test_broken_preview_never_raises(monkeypatch):
    # Любая неожиданная поломка парсера — это error-превью, а не 500 в чате.
    monkeypatch.setattr(
        preview, "_open",
        lambda url: contextlib.nullcontext(
            FakeResponse(url=url, body=b"<html><title>" + b"\xff" * 300)
        ),
    )
    result = preview.get_preview("https://weird.example/")
    assert result["kind"] in ("page", "error")


# ---------------------------------------------------------------------------
# Small units
# ---------------------------------------------------------------------------


def test_upgrade_to_https_only_for_the_same_host():
    assert preview._upgrade_to_https(
        "https://a.example/p", "http://a.example/i.png"
    ) == "https://a.example/i.png"
    assert preview._upgrade_to_https(
        "https://a.example/p", "http://other.example/i.png"
    ) == "http://other.example/i.png"


def test_transient_connection_failure_is_retried_once(monkeypatch):
    """Медленные хосты роняют первое соединение — один повтор их спасает."""
    import requests as requests_lib

    calls = []

    def flaky_open(url):
        calls.append(url)
        if len(calls) == 1:
            raise requests_lib.ConnectionError("dropped")
        return contextlib.nullcontext(
            FakeResponse(url=url, body=b"<title>ok</title>")
        )

    monkeypatch.setattr(preview, "_open", flaky_open)
    result = preview.get_preview("https://flaky.example/page")
    assert result["kind"] == "page"
    assert result["title"] == "ok"
    assert len(calls) == 2


def test_http_errors_are_not_retried(monkeypatch):
    """Протухшая ссылка (404) — это словарь, а не исключение: хватит одного
    запроса, повтор только уронил бы медленный сервер."""
    calls = []

    def open_404(url):
        calls.append(url)
        return contextlib.nullcontext(FakeResponse(url=url, status=404))

    monkeypatch.setattr(preview, "_open", open_404)
    result = preview.get_preview("https://gone.example/abc")
    assert result == {"kind": "error", "reason": "http 404"}
    assert len(calls) == 1
