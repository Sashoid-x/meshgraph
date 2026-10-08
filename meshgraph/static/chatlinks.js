/* Pure helpers for chat link previews: message segmentation and collage
   sizing. Kept free of the DOM so the tests can run them through QuickJS. */

"use strict";

// Break a message into ordered text/link segments, keeping the original
// order («текст ссылка текст» stays exactly that).
function meshgraphSplitLinks(text) {
  const segments = [];
  if (!text) return segments;
  const re = /https?:\/\/[^\s<>"']+/gi;
  let last = 0;
  let match;
  while ((match = re.exec(text)) !== null) {
    const url = meshgraphTrimUrl(match[0]);
    if (!url) continue;
    if (match.index > last) {
      segments.push({ type: "text", text: text.slice(last, match.index) });
    }
    segments.push({ type: "link", url });
    last = match.index + url.length;
  }
  if (last < text.length) segments.push({ type: "text", text: text.slice(last) });
  return segments;
}

// Strip punctuation that trails the link in running text («…а вот
// https://x.ru.»), but keep closing brackets the URL itself unbalances.
function meshgraphTrimUrl(url) {
  let end = url.length;
  while (end > 0 && ".,;:!?".indexOf(url[end - 1]) !== -1) end--;
  const pairs = [["(", ")"], ["[", "]"], ["{", "}"]];
  for (const [open, close] of pairs) {
    let balance = 0;
    for (const ch of url) {
      if (ch === open) balance++;
      else if (ch === close) balance--;
    }
    while (balance < 0 && end > 0 && url[end - 1] === close) {
      end--;
      balance++;
    }
  }
  return url.slice(0, end);
}

// How many pictures a collage shows: up to six cells, beyond that five
// pictures plus a «+N» tile (the lightbox still opens the full list).
function meshgraphCollageGrid(count) {
  if (count <= 6) return { shown: count, extra: 0 };
  return { shown: 5, extra: count - 5 };
}

// Host for a link chip (browser-only helper kept out of app.js for tests).
function meshgraphLinkHost(url) {
  const match = /^[a-z]+:\/\/([^/:?#]+)/i.exec(url);
  return match ? match[1] : url;
}
