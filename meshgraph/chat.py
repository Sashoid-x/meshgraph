"""Channel chat: text and pixel-art messages with replies and reactions.

Meshtastic carries chat as ``TEXT_MESSAGE_APP`` packets.  Two fields of the
inner ``Data`` protobuf turn that into a messenger:

``reply_id``
    "this message is intended to be a reply to a previously sent message
    with the defined id" — rendered as a quote under the answer.

``emoji``
    "payload should be treated as an emoji like giving a message a heart" —
    together with ``reply_id`` it becomes a reaction pill on the target.

Gateways hear the same broadcast independently, so one message usually
arrives several times with different RSSI.  Receptions are merged by
(sender, packet id), keeping the earliest one.  Only broadcast messages are
chat: a direct message addressed to a node is not part of the channel.

Pixel art rides the same channel on ``PRIVATE_APP``: the payload *is* the
picture (header byte, compressed bits, palette trailer — see
``meshgraph/pixelart.py``).  Such rows enter the flow like any other
message and carry an ``image`` field instead of text.
"""

from __future__ import annotations

import time
from typing import Any

from . import pixelart, store
from .config import Settings
from .graph import display_name, sanitize_hours
from .traceroute import BROADCAST_NODE_ID

DEFAULT_LIMIT = 200
MAX_LIMIT = 1000
MAX_TEXT_CHARS = 500

# Codepoint ranges covering the emoji blocks (plus their legacy singles such
# as © and ⌚).  Keycap sequences like "5️⃣" contain a plain digit, so the
# base character is allowed separately when the keycap mark is present.
_EMOJI_RANGES = (
    (0x00A9, 0x00A9),
    (0x00AE, 0x00AE),
    (0x203C, 0x2049),
    (0x2122, 0x2122),
    (0x2139, 0x2139),
    (0x2194, 0x21AA),
    (0x231A, 0x231B),
    (0x2328, 0x2328),
    (0x23CF, 0x23FA),
    (0x24C2, 0x24C2),
    (0x25AA, 0x25AB),
    (0x25B6, 0x25B6),
    (0x25C0, 0x25C0),
    (0x25FB, 0x25FE),
    (0x2600, 0x27BF),
    (0x2934, 0x2935),
    (0x2B05, 0x2B07),
    (0x2B1B, 0x2B1C),
    (0x2B50, 0x2B50),
    (0x2B55, 0x2B55),
    (0x3030, 0x3030),
    (0x303D, 0x303D),
    (0x3297, 0x3297),
    (0x3299, 0x3299),
    (0x1F000, 0x1FAFF),
)
_EMOJI_GLUE = frozenset("\u200d\ufe0e\ufe0f\u20e3")  # ZWJ, variation, keycap
_KEYCAP_BASE = frozenset("#*0123456789")

_COLUMNS = """
    id, timestamp, from_node_id, to_node_id, mesh_packet_id,
    channel_id, portnum_name, reply_id, emoji, raw_payload
"""

_SELECT_TEXT = f"""
    SELECT {_COLUMNS}
    FROM packets
    WHERE portnum_name = 'TEXT_MESSAGE_APP'
      AND processed = 1
      AND (to_node_id IS NULL OR to_node_id = ?)
"""

# Пиксель-арт едет тем же широковещанием, но своим портом: полезный груз
# целиком лежит в raw_payload, поэтому картинки собираются отдельным
# запросом и схлопываются с текстом по (отправитель, id пакета).
_SELECT_PIXEL_ART = f"""
    SELECT {_COLUMNS}
    FROM packets
    WHERE portnum_name = 'PRIVATE_APP'
      AND processed = 1
      AND (to_node_id IS NULL OR to_node_id = ?)
"""


def _clean_text(raw: bytes) -> str:
    """Decode attacker-influenced bytes into renderable, bounded text.

    Newlines format the message and ZWJ joins emoji sequences, so both are
    kept; every other control character becomes the replacement mark, and the
    length is capped so a hostile publisher cannot push megabytes into the
    page.
    """
    text = raw.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")
    keep = "\n\u200d"
    text = "".join(ch if ch.isprintable() or ch in keep else "\uFFFD" for ch in text)
    return text[:MAX_TEXT_CHARS]


def is_emoji_only(text: str) -> bool:
    """True when the message consists of nothing but emoji and their glue."""
    stripped = text.strip()
    if not stripped:
        return False
    keycap = "\u20e3" in stripped
    for ch in stripped:
        if ch in _EMOJI_GLUE or ch.isspace():
            continue
        if keycap and ch in _KEYCAP_BASE:
            continue
        code = ord(ch)
        if not any(lo <= code <= hi for lo, hi in _EMOJI_RANGES):
            return False
    return True


def _row_to_message(row: Any) -> dict[str, Any] | None:
    if row["portnum_name"] == "PRIVATE_APP":
        image = pixelart.decode(row["raw_payload"] or b"")
        if image is None:
            return None  # чужой приватный трафик (MFT и прочее) — не чат
        return {
            "id": row["id"],
            "packet_id": row["mesh_packet_id"] or None,
            "ts": row["timestamp"],
            "from": row["from_node_id"],
            "channel": row["channel_id"],
            "text": "",
            "image": image,
            "emoji_only": False,
            "_reply_id": row["reply_id"] or None,
            "_emoji": row["emoji"],
        }
    text = _clean_text(row["raw_payload"] or b"")
    return {
        "id": row["id"],
        "packet_id": row["mesh_packet_id"] or None,
        "ts": row["timestamp"],
        "from": row["from_node_id"],
        "channel": row["channel_id"],
        "text": text,
        "emoji_only": is_emoji_only(text),
        "_reply_id": row["reply_id"] or None,
        "_emoji": row["emoji"],
    }


def _remember(
    messages: dict[tuple, dict[str, Any]], key: tuple, row: Any
) -> None:
    """Store a reception, keeping the earliest one for the same message."""
    message = _row_to_message(row)
    if message is None:
        return  # приватная строка без валидного пиксель-арта
    existing = messages.get(key)
    if existing is None or row["timestamp"] < existing["ts"]:
        messages[key] = message


def _aggregate_reactions(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One pill per emoji; a node repeating the same reaction counts once."""
    order: list[str] = []
    groups: dict[str, dict[str, Any]] = {}
    for entry in sorted(entries, key=lambda m: (m["ts"], m["id"])):
        emoji = entry["text"]
        group = groups.get(emoji)
        if group is None:
            group = {"nodes": [], "names": []}
            groups[emoji] = group
            order.append(emoji)
        if entry["from"] in group["nodes"]:
            continue
        group["nodes"].append(entry["from"])
        group["names"].append(entry["name"])
    pills = []
    for emoji in order:
        group = groups[emoji]
        pills.append(
            {"emoji": emoji, "count": len(group["nodes"]), "names": group["names"]}
        )
    return pills


def _phantom_message(target: int, entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Stand-in for a message the database never received.

    Reactions to it gather here as pills instead of each one becoming a
    separate "reply" message.  The id is negative so it cannot collide
    with a real packet row; the timestamp is the first reaction — the
    moment the missing message showed up in the channel flow.
    """
    first = min(entries, key=lambda entry: (entry["ts"], entry["id"]))
    return {
        "id": -(int(target) + 1),
        "packet_id": target,
        "ts": first["ts"],
        "from": None,
        "name": None,
        "hex_id": None,
        "channel": first["channel"],
        "text": "",
        "emoji_only": False,
        "phantom": True,
        "_reply_id": None,
    }


def build_chat(
    settings: Settings,
    hours: int = 24,
    channel: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Chat payload: deduplicated messages with quotes and reaction pills."""
    hours = sanitize_hours(hours)
    try:
        limit = int(limit)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    limit = limit if 1 <= limit <= MAX_LIMIT else DEFAULT_LIMIT
    channel = (channel or "").strip() or None
    since = time.time() - hours * 3600

    def select(base_sql: str) -> list[Any]:
        sql = base_sql + " AND timestamp >= ?"
        params: list[Any] = [BROADCAST_NODE_ID, since]
        if channel:
            sql += " AND channel_id = ?"
            params.append(channel)
        sql += " ORDER BY timestamp DESC, id DESC LIMIT ?"
        # Запас на дубли приёмов: в окне должно поместиться `limit` сообщений,
        # а не строк (одно сообщение слышат несколько шлюзов сразу).
        params.append(limit * 3)
        return store.query(settings.db_file, sql, params)

    rows = select(_SELECT_TEXT) + select(_SELECT_PIXEL_ART)

    # One message, many receptions: keep the earliest row per (sender, id).
    messages: dict[tuple, dict[str, Any]] = {}
    for row in rows:
        if row["mesh_packet_id"]:
            key: tuple = ("p", row["from_node_id"], row["mesh_packet_id"])
        else:
            key = ("r", row["id"])
        _remember(messages, key, row)
    if len(messages) > limit:
        freshest = sorted(
            messages.items(),
            key=lambda kv: (kv[1]["ts"], kv[1]["id"]),
            reverse=True,
        )[:limit]
        messages = dict(freshest)

    # Quotes and reactions may point outside the window — pull those targets
    # in, otherwise there is nothing to attach them to.
    referenced = {m["_reply_id"] for m in messages.values() if m["_reply_id"]}
    have = {m["packet_id"] for m in messages.values() if m["packet_id"]}
    missing = referenced - have
    if missing:
        placeholders = ",".join("?" for _ in missing)
        for base_sql in (_SELECT_TEXT, _SELECT_PIXEL_ART):
            target_sql = base_sql + f" AND mesh_packet_id IN ({placeholders})"
            target_params: list[Any] = [BROADCAST_NODE_ID, *sorted(missing)]
            if channel:
                target_sql += " AND channel_id = ?"
                target_params.append(channel)
            for row in store.query(settings.db_file, target_sql, target_params):
                key = ("p", row["from_node_id"], row["mesh_packet_id"])
                _remember(messages, key, row)

    # Names for every author, including the ones quoted from outside the
    # window (their author may differ from the replier).
    authors = {m["from"] for m in messages.values() if m["from"] is not None}
    lookup = store.node_lookup(settings.db_file, sorted(authors))
    for msg in messages.values():
        if msg["from"] is None:
            msg["name"], msg["hex_id"] = "—", None
            continue
        info = lookup.get(msg["from"])
        msg["name"] = display_name(info, msg["from"])
        msg["hex_id"] = (info or {}).get("hex_id") or f"!{msg['from'] & 0xFFFFFFFF:08x}"

    messages_by_pid = {m["packet_id"]: m for m in messages.values() if m["packet_id"]}

    # Split into regular messages and reactions.  A reaction always buckets
    # by its target: when the database never received that target it gets a
    # phantom host below instead of turning into a separate reply message.
    reaction_buckets: dict[int, list[dict[str, Any]]] = {}
    plain: list[dict[str, Any]] = []
    for msg in messages.values():
        target = msg["_reply_id"]
        is_reaction = (
            bool(target)
            and not msg.get("image")  # картинка — не эмодзи-реакция
            and (bool(msg["_emoji"]) or msg["emoji_only"])
        )
        if is_reaction:
            reaction_buckets.setdefault(target, []).append(msg)
        else:
            plain.append(msg)

    for msg in plain:
        target_id = msg["_reply_id"]
        if not target_id:
            continue
        target_msg = messages_by_pid.get(target_id)
        if target_msg is not None:
            target_text = target_msg["text"]
            if not target_text and target_msg.get("image"):
                picture = target_msg["image"]
                target_text = f"пиксель-арт {picture['w']}×{picture['h']}"
            msg["reply_to"] = {
                "packet_id": target_id,
                "from": target_msg["from"],
                "name": target_msg["name"],
                "text": target_text,
                "ts": target_msg["ts"],
            }
        else:
            # The target never reached the database (pruned, or never seen) —
            # the front end shows a "message unavailable" stub for it.
            msg["reply_to"] = {"packet_id": target_id}

    # Reactions land on their host; a host the database never received
    # (pruned, or heard only as a non-text packet) becomes a phantom — one
    # gray stub per missing message carrying all of its reaction pills.
    displayed_by_pid = {m["packet_id"]: m for m in plain if m["packet_id"]}
    phantoms: list[dict[str, Any]] = []
    for target, entries in reaction_buckets.items():
        host = displayed_by_pid.get(target)
        if host is None:
            host = _phantom_message(target, entries)
            phantoms.append(host)
        host["reactions"] = _aggregate_reactions(entries)

    displayed = plain + phantoms
    displayed.sort(key=lambda m: (m["ts"], m["id"]))
    out: list[dict[str, Any]] = []
    for m in displayed:
        out.append(
            {
                "id": m["id"],
                "packet_id": m["packet_id"],
                "ts": m["ts"],
                "from": m["from"],
                "name": m["name"],
                "hex_id": m["hex_id"],
                "channel": m["channel"],
                "text": m["text"],
                "image": m.get("image"),
                "emoji_only": m["emoji_only"],
                "phantom": bool(m.get("phantom")),
                "reply_to": m.get("reply_to"),
                "reactions": m.get("reactions"),
            }
        )
    return {"messages": out, "generated_at": time.time()}
