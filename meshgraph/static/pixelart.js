/* Pixel Art helpers for the chat: palettes, integer scaling, bit unpacking.
 *
 * The picture travels as base64 of a 1-bit row-major bitmap (MSB first) —
 * the browser repaints it on a canvas using the palette that came with the
 * packet.  Pure functions and data, no DOM: executed as-is by the QuickJS
 * unit tests in tests/test_pixelart_js.py (same pattern as layout.js).
 */

"use strict";

/**
 * The 24 palettes from the Pixel Art specification (section 4): background
 * and stroke colours in RGB888.  The picture keeps its own palette in both
 * page themes — dark/light only frames it (see .chat-pixelart in style.css).
 */
const MESHGRAPH_PIXEL_PALETTES = [
  { name: "Classic",         bg: "#ffffff", fg: "#000000" },
  { name: "Classic Dark",    bg: "#000000", fg: "#ffffff" },
  { name: "E-Paper",         bg: "#f5efeb", fg: "#2c2420" },
  { name: "Sepia",           bg: "#eadcc9", fg: "#4a3525" },
  { name: "Blueprint",       bg: "#0a2e5c", fg: "#e0f0ff" },
  { name: "Game Boy",        bg: "#8b956d", fg: "#0f380f" },
  { name: "Game Boy Pocket", bg: "#c4bebb", fg: "#2c2c2c" },
  { name: "Matrix Green",    bg: "#0a0f0d", fg: "#00ff66" },
  { name: "Amber CRT",       bg: "#140a00", fg: "#ffb000" },
  { name: "Solarized Light", bg: "#fdf6e3", fg: "#657b83" },
  { name: "Solarized Dark",  bg: "#002b36", fg: "#2aa198" },
  { name: "Cyberpunk",       bg: "#0b001a", fg: "#ff007f" },
  { name: "Synthwave",       bg: "#1a0a2a", fg: "#00f0ff" },
  { name: "Ocean Blue",      bg: "#001428", fg: "#00d2ff" },
  { name: "Forest Moss",     bg: "#0d1a10", fg: "#a8e063" },
  { name: "Blood Moon",      bg: "#150000", fg: "#ff3333" },
  { name: "Sunset Gold",     bg: "#1a091a", fg: "#ffaa33" },
  { name: "Nordic Frost",    bg: "#2e3440", fg: "#88c0d0" },
  { name: "Dracula",         bg: "#282a36", fg: "#bd93f9" },
  { name: "Chalkboard",      bg: "#233227", fg: "#e8f5e9" },
  { name: "Monokai",         bg: "#272822", fg: "#e6db74" },
  { name: "Terminal White",  bg: "#0f0f0f", fg: "#f0f0f0" },
  { name: "Notebook",        bg: "#faf8f5", fg: "#1a365d" },
  { name: "Graphite",        bg: "#eceff1", fg: "#37474f" },
];

// Пузырь чата 340px: минус аватар (30) с отступом (8), поля пузыря и рамка
// картина — остаётся 274px; ввысь ограничиваем, чтобы картинка не съедала
// окно целиком.  Масштаб всегда целый — тогда пиксели остаются квадратными.
const MESHGRAPH_PIXEL_MAX_W = 274;
const MESHGRAPH_PIXEL_MAX_H = 260;
const MESHGRAPH_PIXEL_MAX_SCALE = 6;

/** Целочисленный масштаб картинки под указанный прямоугольник. */
function meshgraphPixelScale(w, h, maxW, maxH, maxScale) {
  if (!(w > 0) || !(h > 0)) return 1;
  return Math.max(
    1,
    Math.min(maxScale, Math.floor(maxW / w), Math.floor(maxH / h))
  );
}

/**
 * base64 → массив байтов.  Свой разбор вместо atob: работает и в браузере,
 * и в QuickJS, где atob нет.
 */
function meshgraphB64Bytes(text) {
  const alphabet =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  const out = [];
  let buffer = 0;
  let bits = 0;
  for (const ch of text || "") {
    if (ch === "=") break;
    const value = alphabet.indexOf(ch);
    if (value < 0) continue;
    buffer = ((buffer << 6) | value) >>> 0;
    bits += 6;
    if (bits >= 8) {
      bits -= 8;
      out.push((buffer >> bits) & 0xff);
      buffer &= (1 << bits) - 1;
    }
  }
  return out;
}

/**
 * Картинка {w, h, bits} → массив 0/1 длиной w*h в порядке строк
 * (row-major, MSB первый байт) — ровно как в сетевой раскладке.
 */
function meshgraphDecodePixels(image) {
  const count = (image.w * image.h) | 0;
  const bytes = meshgraphB64Bytes(image.bits);
  const pixels = new Array(count);
  for (let i = 0; i < count; i++) {
    const byte = bytes[i >> 3];
    pixels[i] = byte === undefined ? 0 : (byte >> (7 - (i & 7))) & 1;
  }
  return pixels;
}
