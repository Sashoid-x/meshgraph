"""Pixel Art packets on ``PRIVATE_APP`` (``meshgraph/pixelart.py``).

Two kinds of vectors: a real packet captured from the network (DELTA_2D,
32×48, Amber CRT) and hand-built payloads for every encoding — the spec
(section 3) defines each bitstream, so tests encode what the Android app
would send and assert the decoder reproduces the picture byte for byte.
"""

from __future__ import annotations

import base64

from meshgraph import pixelart

# Настоящий пакет из сети Саратова: enc=5 (DELTA_2D), пресет=3 (32×48),
# трейлер 0x08 → палитра 8 (Amber CRT), сетки нет. Слышан одним шлюзом.
GOLDEN_PAYLOAD = bytes.fromhex(
    "537100D673C552058656B04B55C602B0AC92A8CE314228C8002A3894D42A065E"
    "2020455A38485164442864158D6717A44D324A082034182A0444B00A1320454C"
    "60455E27301934C8000C814A8A988C328062A0C1007901000818020401948226"
    "380D65200CB0311526219EB97CD50008"
)


# ---------------------------------------------------------------------------
# Кодировщики: обратная сторона спецификации, чтобы лепить векторы
# ---------------------------------------------------------------------------

def bits_to_bytes(bits: str) -> bytes:
    padded = bits + "0" * (-len(bits) % 8)
    return bytes(int(padded[i : i + 8], 2) for i in range(0, len(padded), 8))


def make_payload(enc: int, preset: int, body_bits: str, trailer: int | None = None) -> bytes:
    body = bits_to_bytes(body_bits)
    if trailer is not None:
        body += bytes([trailer])
    return bytes([(enc << 4) | preset]) + body


def fmt(value: int, width: int) -> str:
    return format(value, f"0{width}b")


def rle_run(run: int) -> str:
    """Префиксный код длины прогона — точная инверсия readVarRleRun."""
    if run == 1:
        return "00"
    if run <= 3:
        return "01" + fmt(run - 2, 1)
    if run <= 7:
        return "100" + fmt(run - 4, 2)
    if run <= 15:
        return "101" + fmt(run - 8, 3)
    if run <= 31:
        return "1100" + fmt(run - 16, 4)
    if run <= 63:
        return "1101" + fmt(run - 32, 5)
    if run <= 255:
        return "1110" + fmt(run - 64, 8)
    if run <= 4351:
        return "1111" + fmt(run - 256, 12)
    raise ValueError(f"прогон {run} длиннее предела кодирования")


def encode_var_rle(values: list[int]) -> str:
    """Смена цвета после каждого прогона — ровно, как читает декодер."""
    bits = "1" if values[0] else "0"
    color = 1 if values[0] else 0
    i = 0
    while i < len(values):
        j = i
        while j < len(values) and (1 if values[j] else 0) == color:
            j += 1
        bits += rle_run(j - i)
        i = j
        color = 1 - color
    return bits


def tile_of(px: list[int], w: int, x0: int, y0: int, size: int) -> list[int]:
    """Пиксели тайла в порядке чтения декодера: строки, только внутри холста."""
    out = []
    for py in range(size):
        for px_x in range(size):
            x, y = x0 + px_x, y0 + py
            if x < w and y < len(px) // w:
                out.append(px[y * w + x])
    return out


def encode_block4(px: list[int], w: int, h: int) -> str:
    bits = ""
    for by in range((h + 3) // 4):
        for bx in range((w + 3) // 4):
            tile = tile_of(px, w, bx * 4, by * 4, 4)
            if not any(tile):
                bits += "0"
            elif all(tile):
                bits += "10"
            else:
                bits += "11" + "".join("1" if v else "0" for v in tile)
    return bits


def encode_block8(px: list[int], w: int, h: int) -> str:
    bits = ""
    for by in range((h + 7) // 8):
        for bx in range((w + 7) // 8):
            tile = tile_of(px, w, bx * 8, by * 8, 8)
            if not any(tile):
                bits += "0"
                continue
            if all(tile):
                bits += "10"
                continue
            bits += "11"
            for sub_y in range(2):
                for sub_x in range(2):
                    sub = tile_of(px, w, bx * 8 + sub_x * 4, by * 8 + sub_y * 4, 4)
                    if not sub or not any(sub):
                        bits += "0"
                    elif all(sub):
                        bits += "10"
                    else:
                        bits += "11" + "".join("1" if v else "0" for v in sub)
    return bits


def predict(x: int, y: int, px: list[int], w: int) -> int:
    """Медианный предиктор по спецификации (раздел 3.6)."""
    if x == 0 and y == 0:
        return 0
    if y == 0:
        return px[x - 1]
    if x == 0:
        return px[(y - 1) * w]
    left = px[y * w + (x - 1)]
    top = px[(y - 1) * w + x]
    diag = px[(y - 1) * w + (x - 1)]
    return left if left == top else (diag ^ left ^ top)


def encode_delta(px: list[int], w: int, h: int) -> str:
    residuals = [px[y * w + x] ^ predict(x, y, px, w) for y in range(h) for x in range(w)]
    return encode_var_rle(residuals)


def lzss_literals(data: bytes) -> str:
    return "".join("0" + fmt(byte, 8) for byte in data)


def unpack(img: dict) -> list[int]:
    raw = base64.b64decode(img["bits"])
    return [(raw[i >> 3] >> (7 - (i & 7))) & 1 for i in range(img["w"] * img["h"])]


# ---------------------------------------------------------------------------
# Золотой вектор: пакет, реально пролетевший по радио
# ---------------------------------------------------------------------------

def test_golden_packet_from_the_network():
    img = pixelart.decode(GOLDEN_PAYLOAD)

    assert img is not None
    assert (img["w"], img["h"]) == (32, 48)
    assert img["theme"] == 8          # Amber CRT
    assert img["grid"] is False

    raw = base64.b64decode(img["bits"])
    assert len(raw) == 192            # ceil(1536 / 8)
    assert sum(bin(byte).count("1") for byte in raw) == 589

    px = unpack(img)
    assert set(px[:32]) == {0}          # верхние строки пустые
    assert px[3 * 32 : 3 * 32 + 5] == [1, 1, 1, 1, 0]


def test_decoding_is_cached_but_returns_fresh_dicts():
    first = pixelart.decode(GOLDEN_PAYLOAD)
    second = pixelart.decode(GOLDEN_PAYLOAD)
    assert first == second and first is not second


# ---------------------------------------------------------------------------
# Распознавание
# ---------------------------------------------------------------------------

def test_mft_file_transfer_is_not_pixel_art():
    assert not pixelart.is_pixel_art(b"MFT\x01" + b"\x00" * 10)
    assert pixelart.decode(b"MFT\x01" + b"\x00" * 10) is None


def test_foreign_private_traffic_is_rejected():
    assert not pixelart.is_pixel_art(b"")            # короче двух байт
    assert not pixelart.is_pixel_art(b"\x00")
    assert not pixelart.is_pixel_art(b"\x70\x00")    # enc = 7 > 6
    assert not pixelart.is_pixel_art(b"\x0a\x00")    # пресет 10 > 9
    assert pixelart.is_pixel_art(GOLDEN_PAYLOAD)


def test_every_preset_reports_its_size():
    for preset, (w, h) in enumerate(pixelart.PRESETS):
        payload = make_payload(0, preset, "0" * (w * h))
        img = pixelart.decode(payload)
        assert (img["w"], img["h"]) == (w, h), preset
        assert img["theme"] == 0 and img["grid"] is False  # legacy без трейлера


# ---------------------------------------------------------------------------
# Каждый алгоритм: закодировали сами — декодер обязан вернуть картинку
# ---------------------------------------------------------------------------

def test_enc_raw_roundtrip_with_trailer_and_legacy():
    preset, (w, h) = 1, (32, 32)
    px = [(x ^ y) & 1 for y in range(h) for x in range(w)]
    bits = "".join(str(v) for v in px)

    with_trailer = pixelart.decode(make_payload(0, preset, bits, trailer=0x80 | 5))
    assert with_trailer["theme"] == 5 and with_trailer["grid"] is True
    assert unpack(with_trailer) == px

    legacy = pixelart.decode(make_payload(0, preset, bits))
    assert legacy["theme"] == 0 and legacy["grid"] is False
    assert unpack(legacy) == px


def test_trailer_theme_out_of_range_falls_back_to_classic():
    payload = make_payload(0, 1, "0" * 1024, trailer=30)
    img = pixelart.decode(payload)
    assert img["theme"] == 0 and img["grid"] is False


def test_enc_block_4x4_roundtrip_with_partial_edge_tiles():
    preset, (w, h) = 0, (39, 40)  # 39 не делится на 4 — краевые тайлы обрезаны
    px = [1 if (x // 3 + y // 5) % 3 else 0 for y in range(h) for x in range(w)]
    for y in range(4):                # угловой тайл — сплошные единицы
        for x in range(4):
            px[y * w + x] = 1
    img = pixelart.decode(make_payload(1, preset, encode_block4(px, w, h), trailer=7))
    assert img["theme"] == 7
    assert unpack(img) == px


def test_enc_block_8x8_roundtrip_with_solid_tiles():
    preset, (w, h) = 1, (32, 32)
    px = [(x * y) % 5 < 2 for y in range(h) for x in range(w)]
    px[: 8 * w] = [0] * (8 * w)   # верхние блоки в нулях (сплошные тайлы)
    for y in range(8):            # блок (1,0) — целиком единицы
        for x in range(8, 16):
            px[y * w + x] = 1
    img = pixelart.decode(make_payload(2, preset, encode_block8(px, w, h)))
    assert unpack(img) == px


def test_enc_var_rle_h_roundtrip():
    preset, (w, h) = 1, (32, 32)
    px = [0] * 40 + [1] * 200 + [0] * 700 + [1] * 36 + [0] * 28 + [1] * 20
    assert len(px) == w * h
    img = pixelart.decode(make_payload(3, preset, encode_var_rle(px), trailer=12))
    assert img["theme"] == 12
    assert unpack(img) == px


def test_enc_var_rle_v_decodes_column_major():
    preset, (w, h) = 3, (32, 48)  # пресет золотого вектора, портрет
    px = [1 if x // 4 == y // 6 else 0 for y in range(h) for x in range(w)]
    # Декодер пишет поток в столбцы и транспонирует — кодируем наоборот.
    column_major = [px[y * w + x] for x in range(w) for y in range(h)]
    img = pixelart.decode(make_payload(4, preset, encode_var_rle(column_major)))
    assert unpack(img) == px


def test_enc_delta_2d_roundtrip():
    preset, (w, h) = 6, (44, 36)
    px = [(y % 7 < 3) * (x % 11 < 6) for y in range(h) for x in range(w)]
    img = pixelart.decode(make_payload(5, preset, encode_delta(px, w, h), trailer=18))
    assert img["theme"] == 18
    assert unpack(img) == px


def test_enc_lzss_literals_roundtrip():
    preset, (w, h) = 1, (32, 32)
    data = bytes((index * 37 + 11) % 256 for index in range(128))  # ceil(1024/8)
    img = pixelart.decode(make_payload(6, preset, lzss_literals(data)))
    raw = base64.b64decode(img["bits"])
    assert raw == data


def test_enc_lzss_sliding_window_copies_the_stream():
    preset = 1  #128 байт ожидаемого вывода
    # 4 литерала, затем окно со смещением 4 и длиной 17: копия зацикливает
    # поток — эталон считается тем же правилом копирования, что в спецификации.
    bits = lzss_literals(bytes([1, 2, 3, 4]))
    bits += "1" + fmt(4 - 1, 6) + fmt(17 - 2, 4)
    img = pixelart.decode(make_payload(6, preset, bits))
    raw = base64.b64decode(img["bits"])

    expected = ([1, 2, 3, 4] * 6)[:21]
    assert list(raw[:21]) == expected
    assert set(raw[21:]) == {0}


# ---------------------------------------------------------------------------
# Устойчивость к мусору
# ---------------------------------------------------------------------------

def test_truncated_packet_still_decodes_safely():
    img = pixelart.decode(GOLDEN_PAYLOAD[:40])
    assert img is not None
    assert (img["w"], img["h"]) == (32, 48)
    assert len(base64.b64decode(img["bits"])) == 192


def test_random_bytes_do_not_raise():
    payload = bytes([0x53]) + bytes(range(60))
    img = pixelart.decode(payload)
    assert img is None or (img["w"], img["h"]) == (32, 48)
