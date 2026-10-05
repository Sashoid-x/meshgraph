"""ServiceEnvelope decoding: plaintext, encrypted, malformed."""

from __future__ import annotations

import base64
import time

import pytest
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from meshtastic import mesh_pb2, mqtt_pb2, portnums_pb2

from meshgraph.config import DEFAULT_CHANNEL_KEY
from meshgraph.decoder import (
    MAX_PAYLOAD_BYTES,
    decode_message,
    hex_id_to_numeric,
    numeric_to_hex_id,
    topic_segments,
)

GATEWAY_HEX = "!11223344"
GATEWAY_ID = 0x11223344
NODE_ID = 0xAABBCCDD
TOPIC = f"msh/US/2/e/LongFast/{GATEWAY_HEX}"


def _encrypt(plaintext: bytes, packet_id: int, sender_id: int, key: bytes) -> bytes:
    nonce = packet_id.to_bytes(8, "little") + sender_id.to_bytes(8, "little")
    cipher = Cipher(algorithms.AES(key), modes.CTR(nonce), backend=default_backend())
    enc = cipher.encryptor()
    return enc.update(plaintext) + enc.finalize()


def _envelope(
    *,
    payload_portnum=portnums_pb2.PortNum.TRACEROUTE_APP,
    payload: bytes = b"\x08\x01",
    encrypted: bytes | None = None,
    hop_limit: int = 3,
    hop_start: int = 3,
    snr: float = 6.5,
    rssi: int = -95,
    channel_id: str = "LongFast",
    reply_id: int = 0,
    emoji: int = 0,
) -> bytes:
    envelope = mqtt_pb2.ServiceEnvelope()
    envelope.channel_id = channel_id
    envelope.gateway_id = GATEWAY_HEX
    packet = envelope.packet
    setattr(packet, "from", NODE_ID)
    packet.to = 0xFFFFFFFF
    packet.id = 4242
    packet.rx_snr = snr
    packet.rx_rssi = rssi
    packet.hop_limit = hop_limit
    packet.hop_start = hop_start
    if encrypted is not None:
        packet.encrypted = encrypted
    else:
        packet.decoded.portnum = payload_portnum
        packet.decoded.payload = payload
        if reply_id:
            packet.decoded.reply_id = reply_id
        if emoji:
            packet.decoded.emoji = emoji
    return envelope.SerializeToString()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_decodes_plaintext_packet():
    packet = decode_message(TOPIC, _envelope(), keys=[])
    assert packet is not None
    assert packet.processed is True
    assert packet.error is None
    assert packet.from_node_id == NODE_ID
    assert packet.to_node_id == 0xFFFFFFFF
    assert packet.portnum_name == "TRACEROUTE_APP"
    assert packet.gateway_id == GATEWAY_HEX
    assert packet.gateway_node_id == GATEWAY_ID
    assert packet.channel_id == "LongFast"
    assert packet.rssi == -95
    assert packet.snr == 6.5
    assert (packet.hop_limit, packet.hop_start) == (3, 3)
    assert packet.raw_payload == b"\x08\x01"
    assert packet.decrypted is False


def test_decrypted_packet_reports_success():
    key = base64.b64decode(DEFAULT_CHANNEL_KEY)
    plaintext = mesh_pb2.Data()
    plaintext.portnum = portnums_pb2.PortNum.TRACEROUTE_APP
    plaintext.payload = b"\x08\x01"
    encrypted = _encrypt(plaintext.SerializeToString(), 4242, NODE_ID, key)

    packet = decode_message(
        TOPIC, _envelope(encrypted=encrypted), keys=[DEFAULT_CHANNEL_KEY]
    )
    assert packet.decrypted is True
    assert packet.processed is True
    assert packet.portnum_name == "TRACEROUTE_APP"
    assert packet.raw_payload == b"\x08\x01"


def test_decrypted_with_default_one_byte_alias():
    """The stock LongFast PSK `AQ==` must decrypt default-key traffic."""
    key = base64.b64decode(DEFAULT_CHANNEL_KEY)  # == firmware defaultpsk
    plaintext = mesh_pb2.Data()
    plaintext.portnum = portnums_pb2.PortNum.TRACEROUTE_APP
    plaintext.payload = b"\x08\x01"
    encrypted = _encrypt(plaintext.SerializeToString(), 4242, NODE_ID, key)

    packet = decode_message(TOPIC, _envelope(encrypted=encrypted), keys=["AQ=="])
    assert packet.decrypted is True
    assert packet.processed is True
    assert packet.raw_payload == b"\x08\x01"


def test_wrong_key_is_reported_not_silently_dropped():
    wrong = base64.b64encode(b"Z" * 32).decode()
    key = base64.b64decode(DEFAULT_CHANNEL_KEY)
    plaintext = mesh_pb2.Data()
    plaintext.portnum = portnums_pb2.PortNum.TRACEROUTE_APP
    plaintext.payload = b"\x08\x01"
    encrypted = _encrypt(plaintext.SerializeToString(), 4242, NODE_ID, key)

    packet = decode_message(TOPIC, _envelope(encrypted=encrypted), keys=[wrong])
    assert packet is not None
    assert packet.decrypted is False
    assert packet.processed is False
    assert "undecryptable" in packet.error


# ---------------------------------------------------------------------------
# Drops
# ---------------------------------------------------------------------------

def test_oversized_payload_is_dropped():
    assert decode_message(TOPIC, b"x" * (MAX_PAYLOAD_BYTES + 1), keys=[]) is None


def test_json_topic_is_dropped():
    assert decode_message("msh/US/2/json/LongFast/!11223344", b"{}", keys=[]) is None


def test_malformed_envelope_sets_error():
    packet = decode_message(TOPIC, b"\xff\xff\xff\xff\xff", keys=[])
    assert packet is not None
    assert packet.processed is False
    assert packet.error


def test_official_topic_segments_are_parsed():
    """``msh/<region>/<version>/<type>/<channel>/<!user>`` → type + channel."""
    packet = decode_message(TOPIC, _envelope(), keys=[])
    assert packet.message_type == "e"
    assert packet.channel_id == "LongFast"


def test_topic_channel_name_drives_key_derivation():
    """The channel name is taken from the 5th topic segment, not guessed."""
    from meshgraph.crypto import derive_key_from_channel_name

    # Payload encrypted with the *derived* LongFast key, but only the base key
    # is configured: decryption can succeed only if the topic segment supplies
    # the channel name.
    derived_key = derive_key_from_channel_name("LongFast", DEFAULT_CHANNEL_KEY)
    plaintext = mesh_pb2.Data()
    plaintext.portnum = portnums_pb2.PortNum.TRACEROUTE_APP
    plaintext.payload = b"\x08\x01"
    encrypted = _encrypt(plaintext.SerializeToString(), 4242, NODE_ID, derived_key)

    matching = decode_message(TOPIC, _envelope(encrypted=encrypted), keys=[DEFAULT_CHANNEL_KEY])
    assert matching.decrypted is True
    assert matching.raw_payload == b"\x08\x01"

    other_topic = f"msh/US/2/e/OtherChannel/{GATEWAY_HEX}"
    mismatched = decode_message(other_topic, _envelope(encrypted=encrypted), keys=[DEFAULT_CHANNEL_KEY])
    assert mismatched.decrypted is False


def test_multisegment_root_topic_is_decoded():
    """ONEmesh roots are `msh/RU/<city>`: two segments precede the version.

    Indices would point at the city code here, so the type and the channel are
    taken from the tail of the topic instead.
    """
    topic = f"msh/RU/SAR/2/e/LongFast/{GATEWAY_HEX}"
    packet = decode_message(topic, _envelope(), keys=[])
    assert packet.message_type == "e"
    assert packet.processed is True
    assert packet.channel_id == "LongFast"


def test_channel_name_may_contain_slashes():
    """A channel literally named `msh/RU/MSK` appears in real ONEmesh traffic."""
    assert topic_segments("msh/RU/MSK/2/e/msh/RU/MSK/!eb60bc92") == (
        "e",
        "msh/RU/MSK",
    )


def test_topic_type_without_channel_segment():
    assert topic_segments(f"msh/RU/SAR/2/pki/{GATEWAY_HEX}") == ("pki", "")


def test_channel_named_like_a_type_token_is_not_confused():
    """`…/2/e/p/!node` — the version segment breaks the tie in favour of `e`."""
    assert topic_segments("msh/RU/SAR/2/e/p/!aabbccdd") == ("e", "p")


# Captured live from mqtt.onemesh.ru: a MapReport from a Samara node.
MAP_REPORT_TOPIC = "msh/RU/SAM/2/map/"
MAP_REPORT_HEX = (
    "0a087361745f33626563120433626563180c206e2a0e322e372e31352e35363762386561"
    "30094d0000b71f550000ed1d5878600f68477001"
)


def test_map_topic_without_node_id_is_recognised():
    assert topic_segments(MAP_REPORT_TOPIC) == ("map", "")


def test_map_report_carries_position():
    """Firmware 2.5+ pushes coordinates to `<root>/<version>/map/`."""
    packet = decode_message(
        MAP_REPORT_TOPIC,
        _envelope(
            payload_portnum=portnums_pb2.PortNum.MAP_REPORT_APP,
            payload=bytes.fromhex(MAP_REPORT_HEX),
        ),
        keys=[],
    )
    assert packet.message_type == "map"
    assert packet.processed is True
    assert packet.error is None
    assert packet.position["latitude"] == pytest.approx(53.2086784, abs=1e-6)
    assert packet.position["longitude"] == pytest.approx(50.2071296, abs=1e-6)


def test_short_topic_yields_no_type():
    assert topic_segments("msh/RU") == (None, "")


def test_legacy_slash_c_topic_is_decoded():
    """Firmware older than 2.3.0 publishes on ``/c/`` instead of ``/e/``."""
    topic = f"msh/EU/2/c/LongFast/{GATEWAY_HEX}"
    packet = decode_message(topic, _envelope(), keys=[])
    assert packet.message_type == "c"
    assert packet.processed is True
    assert packet.channel_id == "LongFast"


# ---------------------------------------------------------------------------
# Side extracts
# ---------------------------------------------------------------------------

def test_position_extracted_into_coordinates():
    position = mesh_pb2.Position(latitude_i=557500000, longitude_i=376000000, altitude=150)
    packet = decode_message(
        TOPIC,
        _envelope(
            payload_portnum=portnums_pb2.PortNum.POSITION_APP,
            payload=position.SerializeToString(),
        ),
        keys=[],
    )
    assert packet.position is not None
    assert packet.position["latitude"] == pytest.approx(55.75)
    assert packet.position["longitude"] == pytest.approx(37.60)
    assert packet.position["altitude"] == 150


def test_nodeinfo_extracted_into_names():
    user = mesh_pb2.User(
        id=GATEWAY_HEX, long_name="Base Station", short_name="BST"
    )
    packet = decode_message(
        TOPIC,
        _envelope(
            payload_portnum=portnums_pb2.PortNum.NODEINFO_APP,
            payload=user.SerializeToString(),
        ),
        keys=[],
    )
    assert packet.node_info is not None
    assert packet.node_info["long_name"] == "Base Station"
    assert packet.node_info["short_name"] == "BST"
    assert packet.node_info["hex_id"] == GATEWAY_HEX


def test_reply_and_emoji_markers_extracted():
    packet = decode_message(
        TOPIC,
        _envelope(
            payload_portnum=portnums_pb2.PortNum.TEXT_MESSAGE_APP,
            payload="👍".encode(),
            reply_id=4242,
            emoji=1,
        ),
        keys=[],
    )
    assert packet.reply_id == 4242
    assert packet.emoji == 1
    assert packet.raw_payload == "👍".encode()


def test_chat_markers_stay_none_when_unset():
    packet = decode_message(TOPIC, _envelope(), keys=[])
    assert packet.reply_id is None
    assert packet.emoji is None


def test_chat_markers_survive_decryption():
    key = base64.b64decode(DEFAULT_CHANNEL_KEY)
    plaintext = mesh_pb2.Data()
    plaintext.portnum = portnums_pb2.PortNum.TEXT_MESSAGE_APP
    plaintext.payload = "привет".encode()
    plaintext.reply_id = 777
    plaintext.emoji = 1
    encrypted = _encrypt(plaintext.SerializeToString(), 4242, NODE_ID, key)

    packet = decode_message(
        TOPIC, _envelope(encrypted=encrypted), keys=[DEFAULT_CHANNEL_KEY]
    )
    assert packet.decrypted is True
    assert packet.reply_id == 777
    assert packet.emoji == 1
    assert packet.raw_payload == "привет".encode()


def test_timestamp_defaults_to_now():
    before = time.time()
    packet = decode_message(TOPIC, _envelope(), keys=[])
    after = time.time()
    assert before <= packet.timestamp <= after


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "hex_id,expected",
    [
        ("!abcdef12", 0xABCDEF12),
        ("abcdef12", 0xABCDEF12),
        ("", None),
        (None, None),
        ("!zzzz", None),
        (123, None),
    ],
)
def test_hex_id_to_numeric(hex_id, expected):
    assert hex_id_to_numeric(hex_id) == expected


def test_hex_id_roundtrip():
    assert numeric_to_hex_id(0xABCDEF12) == "!abcdef12"


def test_sanitize_for_log_strips_control_characters():
    from meshgraph.decoder import sanitize_for_log

    # CWE-117: переводы строк и управляющие символы не должны попадать в лог.
    assert sanitize_for_log("line1\r\nline2") == "line1�\nline2".replace(
        "\n", "�"
    )


def test_sanitize_for_log_truncates_overlong_values():
    from meshgraph.decoder import sanitize_for_log

    assert sanitize_for_log("x" * 500, limit=10) == "x" * 10 + "…"


def test_sanitize_for_log_keeps_plain_text():
    from meshgraph.decoder import sanitize_for_log

    text = "mqtt.onemesh.ru:8883 msh/RU/SAR/#"
    assert sanitize_for_log(text) == text
