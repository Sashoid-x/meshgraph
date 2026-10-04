"""Pixel Art messages on ``PRIVATE_APP``: recognition and decoding.

The companion project (Meshtastic Pixel Art, wire format described in
``PIXEL_ART_FIRMWARE_SPEC.md``) squeezes a 1-bit drawing into a single
private-application packet::

    +--------+---------------------------+--------+
    | Header | compressed pixel bitstream | Trailer|
    +--------+---------------------------+--------+
      enc<<4|preset                      theme|grid

``enc`` selects one of seven encodings (raw, two block schemes, two RLE
flavours, median-predictor deltas, LZSS), ``preset`` selects the canvas
size, and the optional trailing byte carries the palette index and the
pixel-grid preference.  Detection mirrors the firmware reference
(``isPixelArtPacket``): Meshtastic File Transfer shares the port, so the
``MFT\\x01`` magic and out-of-range header nibbles filter it out before
anything is decoded.

Decoded pictures reach the chat as ``{"w", "h", "theme", "grid", "bits"}``
where ``bits`` is base64 of the row-major 1-bit bitmap packed MSB-first —
exactly the ``ENC_RAW`` layout, so the browser unpacks it in a few lines.
"""

from __future__ import annotations

import base64
from functools import lru_cache

# (width, height) per resolution preset — spec section 2.2.
PRESETS: tuple[tuple[int, int], ...] = (
    (39, 40),
    (32, 32),
    (48, 32),
    (32, 48),
    (64, 24),
    (24, 64),
    (44, 36),
    (36, 44),
    (52, 30),
    (30, 52),
)

MAX_PIXELS = 1584  # самый большой пресет (44×36 и 36×44)
MAX_ENCODINGS = 6
MAX_PRESETS = 9
MFT_MAGIC = b"MFT\x01"


def _dims(preset: int) -> tuple[int, int]:
    return PRESETS[preset]


def is_pixel_art(payload: bytes) -> bool:
    """True when the private payload looks like a Pixel Art packet.

    Cheap header check only (same rules as the firmware's ``wantPacket``):
    MFT file-transfer chunks share the port and must not be mistaken for a
    picture, and random private traffic usually fails the nibble bounds.
    """
    if len(payload) < 2:
        return False
    if payload.startswith(MFT_MAGIC):
        return False
    enc = (payload[0] >> 4) & 0x0F
    preset = payload[0] & 0x0F
    return enc <= MAX_ENCODINGS and preset <= MAX_PRESETS


class _BitReader:
    """MSB-first bit reader; reads past the end yield zero bits (as in C++)."""

    __slots__ = ("data", "pos")

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    @property
    def total_bits(self) -> int:
        return len(self.data) * 8

    def has_bits(self) -> bool:
        return self.pos < self.total_bits

    def read_bit(self) -> bool:
        if self.pos >= self.total_bits:
            return False
        byte = self.data[self.pos >> 3]
        bit = (byte >> (7 - (self.pos & 7))) & 1
        self.pos += 1
        return bool(bit)

    def read_bits(self, count: int) -> int:
        value = 0
        for _ in range(count):
            value = (value << 1) | (1 if self.read_bit() else 0)
        return value


def _read_var_rle_run(reader: _BitReader) -> int:
    tag2 = reader.read_bits(2)
    if tag2 == 0b00:
        return 1
    if tag2 == 0b01:
        return 2 + reader.read_bits(1)
    if tag2 == 0b10:
        return 8 + reader.read_bits(3) if reader.read_bit() else 4 + reader.read_bits(2)
    tag4 = reader.read_bits(2)
    if tag4 == 0b00:
        return 16 + reader.read_bits(4)
    if tag4 == 0b01:
        return 32 + reader.read_bits(5)
    if tag4 == 0b10:
        return 64 + reader.read_bits(8)
    return 256 + reader.read_bits(12)


def _decode_var_rle(reader: _BitReader, count: int) -> list[bool]:
    """Alternating run-length stream: colour bit, then runs until `count`."""
    out = [False] * count
    if not reader.has_bits():
        return out
    color = reader.read_bit()
    written = 0
    while written < count and reader.has_bits():
        run = _read_var_rle_run(reader)
        stop = min(written + run, count)
        for index in range(written, stop):
            out[index] = color
        written = stop
        color = not color
    return out


def _decode_block_4x4(
    reader: _BitReader, out: list[bool], width: int, height: int
) -> None:
    for by in range((height + 3) // 4):
        for bx in range((width + 3) // 4):
            start_x, start_y = bx * 4, by * 4
            if not reader.read_bit():
                continue  # тайл сплошных нулей
            all_one = not reader.read_bit()
            if all_one:
                for py in range(4):
                    for px in range(4):
                        x, y = start_x + px, start_y + py
                        if x < width and y < height:
                            out[y * width + x] = True
            else:
                for py in range(4):
                    for px in range(4):
                        x, y = start_x + px, start_y + py
                        if x < width and y < height:
                            out[y * width + x] = reader.read_bit()


def _decode_block_8x8(
    reader: _BitReader, out: list[bool], width: int, height: int
) -> None:
    for by in range((height + 7) // 8):
        for bx in range((width + 7) // 8):
            start_x, start_y = bx * 8, by * 8
            if not reader.read_bit():
                continue  # блок сплошных нулей
            if not reader.read_bit():
                # сплошные единицы
                for py in range(8):
                    for px in range(8):
                        x, y = start_x + px, start_y + py
                        if x < width and y < height:
                            out[y * width + x] = True
                continue
            for sub_y in range(2):
                for sub_x in range(2):
                    sx, sy = start_x + sub_x * 4, start_y + sub_y * 4
                    if not reader.read_bit():
                        continue
                    if not reader.read_bit():
                        for py in range(4):
                            for px in range(4):
                                x, y = sx + px, sy + py
                                if x < width and y < height:
                                    out[y * width + x] = True
                        continue
                    for py in range(4):
                        for px in range(4):
                            x, y = sx + px, sy + py
                            if x < width and y < height:
                                out[y * width + x] = reader.read_bit()


def _predict(x: int, y: int, out: list[bool], width: int) -> bool:
    """2D median predictor (Paeth-like) over the already rebuilt pixels."""
    if x == 0 and y == 0:
        return False
    if y == 0:
        return out[x - 1]
    if x == 0:
        return out[(y - 1) * width]
    left = out[y * width + (x - 1)]
    top = out[(y - 1) * width + x]
    diag = out[(y - 1) * width + (x - 1)]
    return left if left == top else (diag ^ left ^ top)


def _decode_delta_2d(
    reader: _BitReader, out: list[bool], width: int, height: int
) -> None:
    residuals = _decode_var_rle(reader, width * height)
    for y in range(height):
        for x in range(width):
            index = y * width + x
            out[index] = bool(residuals[index]) ^ _predict(x, y, out, width)


def _decode_lzss(reader: _BitReader, out: list[bool], pixel_count: int) -> None:
    expected_bytes = (pixel_count + 7) // 8
    buffer = [0] * max(expected_bytes, 1)
    out_pos = 0
    while out_pos < expected_bytes and reader.has_bits():
        if reader.read_bit():
            offset = reader.read_bits(6) + 1
            length = reader.read_bits(4) + 2
            start = out_pos - offset
            for i in range(length):
                if out_pos < expected_bytes and (start + i) >= 0:
                    buffer[out_pos] = buffer[start + i]
                    out_pos += 1
        else:
            buffer[out_pos] = reader.read_bits(8)
            out_pos += 1
    for index in range(pixel_count):
        byte_index = index >> 3
        if byte_index < expected_bytes:
            out[index] = bool((buffer[byte_index] >> (7 - (index & 7))) & 1)


def _pack(out: list[bool]) -> bytes:
    """Bits → row-major bytes, MSB first (the ENC_RAW wire layout)."""
    packed = bytearray((len(out) + 7) // 8)
    for index, bit in enumerate(out):
        if bit:
            packed[index >> 3] |= 1 << (7 - (index & 7))
    return bytes(packed)


@lru_cache(maxsize=1024)
def _decode(payload: bytes) -> tuple[int, int, int, bool, bytes] | None:
    """Decode one packet body; ``None`` when it is not (usable) pixel art."""
    if not is_pixel_art(payload):
        return None

    enc = (payload[0] >> 4) & 0x0F
    preset = payload[0] & 0x0F
    width, height = _dims(preset)
    pixel_count = width * height
    out = [False] * pixel_count

    body = payload[1:]
    body_len = len(body)
    reader = _BitReader(body)

    if enc == 0:  # ENC_RAW — без ридера, байты читаются напрямую
        for index in range(pixel_count):
            byte_index = index >> 3
            if byte_index < body_len:
                out[index] = bool((body[byte_index] >> (7 - (index & 7))) & 1)
    elif enc == 1:
        _decode_block_4x4(reader, out, width, height)
    elif enc == 2:
        _decode_block_8x8(reader, out, width, height)
    elif enc == 3:
        out = _decode_var_rle(reader, pixel_count)
    elif enc == 4:  # RLE в столбцах → транспонируем в строки
        temp = _decode_var_rle(reader, pixel_count)
        transposed = [False] * pixel_count
        for y in range(height):
            for x in range(width):
                transposed[y * width + x] = temp[x * height + y]
        out = transposed
    elif enc == 5:
        _decode_delta_2d(reader, out, width, height)
    else:  # enc == 6
        _decode_lzss(reader, out, pixel_count)

    # Наличие трейлера: распакованный поток занял меньше байт, чем пришло
    # (раздел 2.3 «Backward Compatibility Rule»).
    consumed = (pixel_count + 7) // 8 if enc == 0 else (reader.pos + 7) // 8
    theme, grid = 0, False
    if body_len > consumed:
        trailer = body[-1]
        grid = bool(trailer & 0x80)
        theme = trailer & 0x7F
        if theme >= 24:
            theme = 0  # запасной вариант спецификации: Classic

    return width, height, theme, grid, _pack(out)


def decode(payload: bytes) -> dict | None:
    """Decoded picture for the chat API, or ``None`` when not pixel art.

    Returns ``{"w", "h", "theme", "grid", "bits"}``; ``bits`` is base64 of
    ``ceil(w*h/8)`` bytes.  Safe on hostile input: recognition bounds are
    checked first and truncated streams decode to whatever fits, exactly
    like the firmware decoder.
    """
    result = _decode(bytes(payload))
    if result is None:
        return None
    width, height, theme, grid, packed = result
    return {
        "w": width,
        "h": height,
        "theme": theme,
        "grid": grid,
        "bits": base64.b64encode(packed).decode("ascii"),
    }
