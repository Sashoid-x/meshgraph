"""Decode a raw Meshtastic MQTT message into a normalised packet record.

Meshtastic gateways publish protobuf ``ServiceEnvelope`` messages on topics
shaped like ``msh/<region>/<version>/<e|c|pki>/<channel>/<!user-id>``, e.g.
``msh/US/2/e/LongFast/!abcd1234`` (``/c/`` on firmware older than 2.3.0).
The envelope wraps a ``MeshPacket`` whose payload may be AES encrypted.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from meshtastic import config_pb2, mesh_pb2, mqtt_pb2, portnums_pb2

from .crypto import try_decrypt_mesh_packet

logger = logging.getLogger(__name__)

# A legit ServiceEnvelope tops out around 1.25 KB; 4 KB stops a publisher on a
# public broker from turning one PUBLISH into an arbitrarily large parse.
MAX_PAYLOAD_BYTES = 4096


def sanitize_for_log(value: object, limit: int = 200) -> str:
    """Strip CR/LF and control chars from attacker-influenced text (CWE-117)."""
    text = str(value)
    if len(text) > limit:
        text = text[:limit] + "…"
    return "".join(ch if ch.isprintable() else "�" for ch in text)


def hex_id_to_numeric(hex_id: Any) -> int | None:
    """Convert ``!abcdef12`` (or a bare hex string) to an integer node id."""
    if not hex_id or not isinstance(hex_id, str):
        return None
    if hex_id.startswith("!"):
        hex_id = hex_id[1:]
    try:
        return int(hex_id, 16)
    except ValueError:
        return None


def numeric_to_hex_id(node_id: int) -> str:
    return f"!{node_id & 0xFFFFFFFF:08x}"


def get_enum_name(descriptor: Any, value: Any) -> str | None:
    """Resolve an enum number to its protobuf name, or ``UNKNOWN_<n>``."""
    if value is None:
        return None
    entry = descriptor.values_by_number.get(value)
    return entry.name if entry else f"UNKNOWN_{value}"


# The topic *root* is user-configurable: `msh/US`, `msh/RU/SAR`, `msh/DE/BY/…`.
# Only the tail is fixed: `<root>/<version>/<type>/<channel>/<node-id>`, so the
# type and the channel are located from the end rather than by index.
_TOPIC_TYPE_TOKENS = frozenset(
    {"e", "c", "p", "pkc", "pki", "json", "x", "crt", "map"}
)


def topic_segments(topic: str) -> tuple[str | None, str]:
    """Return ``(message_type, channel_name)`` for a Meshtastic topic.

    ``msh/US/2/e/LongFast/!abcd`` and ``msh/RU/SAR/2/e/LongFast/!abcd`` both
    yield ``("e", "LongFast")``.  Topics without a trailing node id (the map
    reports ONEmesh publishes as ``msh/RU/SAM/2/map/``) are handled the same
    way; when no type token is recognised the historic fixed positions are
    used as a fallback.
    """
    parts = topic.split("/")
    if len(parts) < 3:
        return None, ""

    has_node_id = parts[-1].startswith("!")
    # Backwards scan for the type token: the channel may itself contain `/`
    # (real traffic has channels named `msh/RU/MSK`), so everything between the
    # type and the node id belongs to the channel.
    scan_from = len(parts) - 2 if has_node_id else len(parts) - 1
    candidates = [
        index
        for index in range(scan_from, 0, -1)
        if parts[index] in _TOPIC_TYPE_TOKENS
    ]
    if candidates:
        # The type follows the protocol version (`…/2/e/…`), which breaks the
        # tie when a channel name collides with a type token.
        versioned = [index for index in candidates if parts[index - 1].isdigit()]
        index = versioned[0] if versioned else candidates[0]
        tail = parts[index + 1 : -1] if has_node_id else parts[index + 1 :]
        return parts[index], "/".join(tail)

    if has_node_id:
        # Unknown type token: assume the classic `<type>/<channel>/<node-id>`.
        return parts[-3], parts[-2]

    # Legacy layout without a node id at the end.
    message_type = parts[3] if len(parts) >= 4 else None
    channel_name = ""
    if len(parts) >= 5 and not parts[4].startswith("!"):
        channel_name = parts[4]
    return message_type, channel_name


@dataclass
class DecodedPacket:
    """Everything the store and the graph builder need about one message."""

    timestamp: float
    topic: str
    message_type: str | None = None

    from_node_id: int | None = None
    to_node_id: int | None = None
    mesh_packet_id: int | None = None

    portnum: int | None = None
    portnum_name: str | None = None

    gateway_id: str | None = None
    gateway_node_id: int | None = None
    channel_id: str | None = None

    rssi: int | None = None
    snr: float | None = None
    hop_limit: int | None = None
    hop_start: int | None = None
    rx_time: int | None = None
    via_mqtt: bool | None = None
    next_hop: int | None = None
    relay_node: int | None = None

    # Chat metadata from the same Data protobuf: which packet this message
    # answers (``reply_id``) and whether its payload is an emoji reaction
    # (``emoji``, "treated as an emoji like giving a message a heart").
    reply_id: int | None = None
    emoji: int | None = None

    payload_length: int = 0
    raw_payload: bytes = b""
    raw_envelope: bytes = b""

    processed: bool = False
    decrypted: bool = False
    error: str | None = None

    # Side-channel extracts ------------------------------------------------
    position: dict[str, float] | None = None
    node_info: dict[str, Any] | None = None

    def to_row(self) -> dict[str, Any]:
        """Columns for the ``packets`` table.

        Side-channel extracts and the raw envelope blob stay out of the row:
        the first is folded into the ``nodes`` table, the second would double
        storage for no reader.
        """
        row = asdict(self)
        for transient in ("position", "node_info", "raw_envelope"):
            row.pop(transient, None)
        return row


def decode_message(
    topic: str,
    payload: bytes,
    keys: list[str],
    now: float | None = None,
) -> DecodedPacket | None:
    """Decode one MQTT PUBLISH.

    Returns ``None`` when the message should be dropped outright (oversized or
    JSON-formatted); otherwise returns a record whose ``error`` field explains
    why it could not be fully parsed, if applicable.
    """
    timestamp = now if now is not None else time.time()

    if len(payload) > MAX_PAYLOAD_BYTES:
        logger.warning(
            "Dropping oversized payload on %s (%s bytes)",
            sanitize_for_log(topic),
            len(payload),
        )
        return None

    if "/json/" in topic:
        return None

    message_type, channel_name = topic_segments(topic)

    packet = DecodedPacket(
        timestamp=timestamp,
        topic=topic,
        message_type=message_type,
        raw_envelope=payload,
    )

    try:
        envelope = mqtt_pb2.ServiceEnvelope()
        envelope.ParseFromString(payload)
        mesh_packet = envelope.packet
    except Exception as exc:  # noqa: BLE001 - garbage from a public broker
        packet.error = f"envelope parse failed: {sanitize_for_log(exc)}"
        return packet

    packet.gateway_id = getattr(envelope, "gateway_id", None) or None
    packet.gateway_node_id = hex_id_to_numeric(packet.gateway_id)
    packet.channel_id = getattr(envelope, "channel_id", None) or None

    packet.from_node_id = getattr(mesh_packet, "from", None)
    packet.to_node_id = mesh_packet.to
    packet.mesh_packet_id = mesh_packet.id
    packet.rssi = getattr(mesh_packet, "rx_rssi", None)
    packet.snr = getattr(mesh_packet, "rx_snr", None)
    packet.hop_limit = getattr(mesh_packet, "hop_limit", None)
    packet.hop_start = getattr(mesh_packet, "hop_start", None)
    packet.rx_time = getattr(mesh_packet, "rx_time", None) or None
    packet.via_mqtt = bool(getattr(mesh_packet, "via_mqtt", False))
    packet.next_hop = getattr(mesh_packet, "next_hop", None)
    packet.relay_node = getattr(mesh_packet, "relay_node", None)

    # --- decryption -------------------------------------------------------
    needs_decrypt = (
        mesh_packet.decoded.portnum == portnums_pb2.PortNum.UNKNOWN_APP
        and bool(mesh_packet.encrypted)
    )
    if needs_decrypt:
        # Primary key first, then the channel-specific derivation.
        if try_decrypt_mesh_packet(mesh_packet, channel_name="", keys_base64=keys):
            packet.decrypted = True
        elif channel_name and try_decrypt_mesh_packet(
            mesh_packet, channel_name=channel_name, keys_base64=keys
        ):
            packet.decrypted = True

    packet.portnum = mesh_packet.decoded.portnum
    packet.portnum_name = get_enum_name(
        portnums_pb2.PortNum.DESCRIPTOR, packet.portnum
    )
    packet.raw_payload = bytes(mesh_packet.decoded.payload or b"")
    packet.payload_length = len(packet.raw_payload)

    # Reply/reaction markers live next to the payload in Data, not in it —
    # they survive decryption above and are 0 whenever unset.
    if getattr(mesh_packet.decoded, "reply_id", 0):
        packet.reply_id = int(mesh_packet.decoded.reply_id)
    if getattr(mesh_packet.decoded, "emoji", 0):
        packet.emoji = 1

    if packet.portnum_name == "UNKNOWN_APP":
        packet.error = packet.error or "undecryptable payload (check channel keys)"

    # --- side extracts ----------------------------------------------------
    try:
        _extract_side_data(packet, mesh_packet)
    except Exception as exc:  # noqa: BLE001 - never fail the whole message
        logger.debug("Side data extraction failed: %s", sanitize_for_log(exc))

    packet.processed = packet.error is None
    return packet


def _extract_side_data(packet: DecodedPacket, mesh_packet: Any) -> None:
    """Pull node names and coordinates out of the payload, if present."""
    portnum = packet.portnum
    raw = packet.raw_payload

    if portnum == portnums_pb2.PortNum.POSITION_APP and raw:
        position = mesh_pb2.Position()
        position.ParseFromString(raw)
        if position.latitude_i or position.longitude_i:
            packet.position = {
                "latitude": position.latitude_i / 1e7,
                "longitude": position.longitude_i / 1e7,
                "altitude": float(position.altitude or 0),
                "timestamp": packet.timestamp,
            }

    elif portnum == portnums_pb2.PortNum.MAP_REPORT_APP and raw:
        # Firmware pushes these to `<root>/<version>/map/`; the coordinates are
        # exactly what the geographic graph layout needs.
        report = mqtt_pb2.MapReport()
        report.ParseFromString(raw)
        if report.latitude_i or report.longitude_i:
            packet.position = {
                "latitude": report.latitude_i / 1e7,
                "longitude": report.longitude_i / 1e7,
                "altitude": float(report.altitude or 0),
                "timestamp": packet.timestamp,
            }

    elif portnum == portnums_pb2.PortNum.NODEINFO_APP and raw:
        user = mesh_pb2.User()
        user.ParseFromString(raw)
        mac = None
        if getattr(user, "macaddr", None):
            try:
                mac = user.macaddr.hex(":")
            except Exception:  # noqa: BLE001
                mac = None
        packet.node_info = {
            "node_id": packet.from_node_id,
            "hex_id": user.id or None,
            "long_name": user.long_name or None,
            "short_name": user.short_name or None,
            "hw_model": get_enum_name(mesh_pb2.HardwareModel.DESCRIPTOR, user.hw_model),
            "role": get_enum_name(
                config_pb2.Config.DeviceConfig.Role.DESCRIPTOR, user.role
            ),
            "primary_channel": packet.channel_id,
        }
