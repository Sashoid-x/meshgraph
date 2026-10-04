"""Pixel Art helpers (``meshgraph/static/pixelart.js``) run through QuickJS.

Как layout.js, логика картинок в чате — чистый модуль: палитры, целочисленный
масштаб и разбор base64/бит выполняются в JS-движке без браузера, а золотой
пакет из сети обязан распаковаться в Python и JS одинаково.
"""

from __future__ import annotations

import base64
import json
import pathlib

import pytest

from meshgraph import pixelart

from .test_pixelart import GOLDEN_PAYLOAD

quickjs = pytest.importorskip("quickjs")

ROOT = pathlib.Path(__file__).resolve().parents[1]
PIXEL_JS = ROOT / "meshgraph" / "static" / "pixelart.js"


@pytest.fixture(scope="module")
def js() -> "quickjs.Context":
    ctx = quickjs.Context()
    ctx.eval(PIXEL_JS.read_text(encoding="utf-8"))
    return ctx


def run(js, expression: str):
    """Evaluate a JS expression and bring the result back as Python data."""
    return json.loads(js.eval(f"JSON.stringify({expression})"))


# ---------------------------------------------------------------------------
# Данные
# ---------------------------------------------------------------------------

def test_twenty_four_palettes(js):
    palettes = run(js, "MESHGRAPH_PIXEL_PALETTES")

    assert len(palettes) == 24
    for palette in palettes:
        assert palette["name"]
        assert palette["bg"].startswith("#") and len(palette["bg"]) == 7
        assert palette["fg"].startswith("#") and len(palette["fg"]) == 7


def test_limits_fit_the_chat_bubble(js):
    assert run(js, "[MESHGRAPH_PIXEL_MAX_W, MESHGRAPH_PIXEL_MAX_H, MESHGRAPH_PIXEL_MAX_SCALE]") == [274, 260, 6]


# ---------------------------------------------------------------------------
# Масштаб
# ---------------------------------------------------------------------------

def test_scale_is_integer_and_fits_every_preset(js):
    expr = """(function (presets) {
        return presets.map(([w, h]) => meshgraphPixelScale(
            w, h, MESHGRAPH_PIXEL_MAX_W, MESHGRAPH_PIXEL_MAX_H,
            MESHGRAPH_PIXEL_MAX_SCALE));
    })(%s)""" % json.dumps(list(pixelart.PRESETS))
    scales = run(js, expr)

    for (w, h), scale in zip(pixelart.PRESETS, scales):
        assert scale == int(scale) >= 1
        assert w * scale <= 274 and h * scale <= 260

    # Примеры: портрет 32×48 упирается в высоту, квадрат 39×40 — в кап.
    assert run(js, "meshgraphPixelScale(32, 48, 274, 260, 6)") == 5
    assert run(js, "meshgraphPixelScale(39, 40, 274, 260, 6)") == 6
    assert run(js, "meshgraphPixelScale(0, 10, 274, 260, 6)") == 1  # вырожденное


# ---------------------------------------------------------------------------
# Разбор base64 и бит
# ---------------------------------------------------------------------------

def test_base64_decoder_handles_padding_and_garbage(js):
    assert run(js, 'meshgraphB64Bytes("oA==")') == [0xA0]
    assert run(js, 'meshgraphB64Bytes("QUI=")') == [65, 66]
    assert run(js, 'meshgraphB64Bytes("AAAA")') == [0, 0, 0]
    assert run(js, 'meshgraphB64Bytes("!!")') == []
    assert run(js, 'meshgraphB64Bytes("")') == []


def test_pixel_unpacking_is_msb_first(js):
    # 0xA0 = 10100000: первая строка из восьми пикселей
    assert run(js, 'meshgraphDecodePixels({w: 8, h: 1, bits: "oA=="})') == [
        1, 0, 1, 0, 0, 0, 0, 0,
    ]
    # Обрезанные данные достраиваются нулями, а не мусором
    assert run(js, 'meshgraphDecodePixels({w: 16, h: 1, bits: "oA=="})') == [
        1, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    ]


def test_golden_packet_unpacks_identically_to_python(js):
    image = pixelart.decode(GOLDEN_PAYLOAD)
    pixels_js = run(js, f"meshgraphDecodePixels({json.dumps(image)})")

    raw = base64.b64decode(image["bits"])
    pixels_py = [
        (raw[i >> 3] >> (7 - (i & 7))) & 1 for i in range(image["w"] * image["h"])
    ]

    assert pixels_js == pixels_py
    assert sum(pixels_js) == 589
