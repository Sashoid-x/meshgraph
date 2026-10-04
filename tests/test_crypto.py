"""AES-256-CTR channel decryption."""

from __future__ import annotations

import base64

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from meshtastic import mesh_pb2, portnums_pb2

from meshgraph.crypto import (
    MESHTASTIC_DEFAULT_PSK,
    decrypt_packet,
    derive_key_from_channel_name,
    normalize_psk,
    try_decrypt_mesh_packet,
)

CHANNEL_KEY = base64.b64encode(b"K" * 32).decode()


def _encrypt(plaintext: bytes, packet_id: int, sender_id: int, key: bytes) -> bytes:
    nonce = packet_id.to_bytes(8, "little") + sender_id.to_bytes(8, "little")
    cipher = Cipher(algorithms.AES(key), modes.CTR(nonce), backend=default_backend())
    encryptor = cipher.encryptor()
    return encryptor.update(plaintext) + encryptor.finalize()


def _sample_data_payload() -> bytes:
    data = mesh_pb2.Data()
    data.portnum = portnums_pb2.PortNum.TRACEROUTE_APP
    data.payload = b"route-bytes"
    return data.SerializeToString()


def test_primary_channel_key_is_used_verbatim():
    key = derive_key_from_channel_name("", CHANNEL_KEY)
    assert key == base64.b64decode(CHANNEL_KEY)


# ---------------------------------------------------------------------------
# PSK normalisation (meshtastic firmware: Channels::getKey)
# ---------------------------------------------------------------------------

def test_normalize_expands_the_default_one_byte_alias():
    # `AQ==` == 0x01 == "use the firmware defaultpsk", which is the 16-byte
    # key behind our own DEFAULT_CHANNEL_KEY.
    assert len(MESHTASTIC_DEFAULT_PSK) == 16
    assert normalize_psk(b"\x01") == MESHTASTIC_DEFAULT_PSK
    assert derive_key_from_channel_name("", "AQ==") == MESHTASTIC_DEFAULT_PSK


def test_normalize_bumps_the_last_byte_of_other_aliases():
    # `simple2` → bytes([2]) → defaultpsk with the last byte moved by 1.
    assert normalize_psk(b"\x02") == MESHTASTIC_DEFAULT_PSK[:-1] + b"\x02"
    assert normalize_psk(b"\x02") != normalize_psk(b"\x01")


def test_normalize_pads_short_keys_the_way_firmware_does():
    assert normalize_psk(b"") == b""
    assert normalize_psk(b"\x00") == b""  # lone zero byte → encryption off
    assert normalize_psk(b"abc") == b"abc".ljust(16, b"\x00")
    assert normalize_psk(b"y" * 16) == b"y" * 16
    assert normalize_psk(b"z" * 20) == b"z" * 20 + b"\x00" * 12
    assert normalize_psk(b"q" * 32) == b"q" * 32


def test_try_decrypt_with_the_default_one_byte_alias():
    payload = _sample_data_payload()
    packet = mesh_pb2.MeshPacket()
    setattr(packet, "from", 7)
    packet.id = 7
    packet.encrypted = _encrypt(payload, 7, 7, MESHTASTIC_DEFAULT_PSK)

    assert try_decrypt_mesh_packet(packet, keys_base64=["AQ=="]) is True
    assert packet.decoded.portnum == portnums_pb2.PortNum.TRACEROUTE_APP


def test_named_channel_key_is_hashed_differently():
    primary = derive_key_from_channel_name("", CHANNEL_KEY)
    named = derive_key_from_channel_name("LongFast", CHANNEL_KEY)
    assert named != primary
    assert len(named) == 32
    # Deterministic for the same channel name.
    assert named == derive_key_from_channel_name("LongFast", CHANNEL_KEY)


def test_decrypt_packet_roundtrip():
    key = base64.b64decode(CHANNEL_KEY)
    payload = _sample_data_payload()
    packet_id, sender_id = 12345, 0xAABBCCDD
    encrypted = _encrypt(payload, packet_id, sender_id, key)

    assert decrypt_packet(encrypted, packet_id, sender_id, key) == payload


def test_decrypt_packet_rejects_empty_and_bad_key():
    key = base64.b64decode(CHANNEL_KEY)
    assert decrypt_packet(b"", 1, 2, key) == b""

    wrong = base64.b64encode(b"X" * 32).decode()
    plaintext = _sample_data_payload()
    encrypted = _encrypt(plaintext, 1, 2, base64.b64decode(wrong))
    # Correct key on wrong ciphertext: parses as garbage → caller falls through,
    # and a literally wrong key must not reproduce the plaintext.
    assert decrypt_packet(encrypted, 1, 2, base64.b64decode(CHANNEL_KEY)) != plaintext


def test_try_decrypt_populates_decoded():
    key = base64.b64decode(CHANNEL_KEY)
    payload = _sample_data_payload()
    packet_id, sender_id = 99, 0x11223344

    packet = mesh_pb2.MeshPacket()
    setattr(packet, "from", sender_id)
    packet.id = packet_id
    packet.encrypted = _encrypt(payload, packet_id, sender_id, key)

    assert packet.decoded.portnum == portnums_pb2.PortNum.UNKNOWN_APP
    assert try_decrypt_mesh_packet(packet, channel_name="", keys_base64=[CHANNEL_KEY]) is True
    assert packet.decoded.portnum == portnums_pb2.PortNum.TRACEROUTE_APP
    assert bytes(packet.decoded.payload) == b"route-bytes"


def test_try_decrypt_tries_keys_in_order():
    payload = _sample_data_payload()
    good = base64.b64encode(b"G" * 32).decode()
    bad = base64.b64encode(b"B" * 32).decode()

    packet = mesh_pb2.MeshPacket()
    setattr(packet, "from", 7)
    packet.id = 7
    packet.encrypted = _encrypt(payload, 7, 7, base64.b64decode(good))

    assert try_decrypt_mesh_packet(packet, keys_base64=[bad, good]) is True
    assert packet.decoded.portnum == portnums_pb2.PortNum.TRACEROUTE_APP


def test_try_decrypt_fails_without_keys():
    payload = _sample_data_payload()
    packet = mesh_pb2.MeshPacket()
    setattr(packet, "from", 7)
    packet.id = 7
    packet.encrypted = _encrypt(payload, 7, 7, b"K" * 32)

    assert try_decrypt_mesh_packet(packet, keys_base64=[]) is False
    assert packet.decoded.portnum == portnums_pb2.PortNum.UNKNOWN_APP


def test_try_decrypt_skips_already_plaintext_packets():
    packet = mesh_pb2.MeshPacket()
    packet.decoded.portnum = portnums_pb2.PortNum.TEXT_MESSAGE_APP
    packet.decoded.payload = b"hello"
    assert try_decrypt_mesh_packet(packet, keys_base64=[CHANNEL_KEY]) is False
