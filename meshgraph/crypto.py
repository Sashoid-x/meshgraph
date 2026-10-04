"""Meshtastic packet decryption (AES-CTR).

Ported from Malla's ``utils/decryption.py`` / ``mqtt_capture.py``: a Meshtastic
channel payload is AES-CTR encrypted with a key derived from the base64 channel
key, using ``packet_id (8 LE bytes) + sender_id (8 LE bytes)`` as the nonce.

Channel PSKs are normalised the way the firmware does it (see
:func:`normalize_psk`), so one-byte aliases such as ``AQ==`` work too.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from typing import Any

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

logger = logging.getLogger(__name__)

# `defaultpsk` from the meshtastic firmware (src/mesh/Channels.h): the 16-byte
# AES-128 key that the one-byte PSK aliases expand into.  Also the key the
# stock LongFast channel encrypts with, hence the project-wide default.
MESHTASTIC_DEFAULT_PSK = base64.b64decode("1PG7OiApB1nwvP+rz05pAQ==")

# nanopb `max_size` of ChannelSettings.psk: the firmware cannot store more.
MAX_PSK_BYTES = 32


def normalize_psk(key: bytes) -> bytes:
    """Expand a channel PSK exactly like the firmware's ``Channels::getKey``.

    Meshtastic accepts PSKs that are not usable as an AES key directly:

    * a single byte is an alias — ``0x01`` (base64 ``AQ==``, the default
      LongFast key) means "use ``defaultpsk``", any other byte bumps the last
      byte of ``defaultpsk`` accordingly;
    * 2..15 bytes are zero-padded to 16 (AES-128);
    * 17..31 bytes are zero-padded to 32 (AES-256).

    An empty key — or one ``0x00`` byte — means "encryption off" and comes
    back as ``b""`` so callers skip it instead of decrypting with garbage.
    """
    if not key:
        return b""
    if len(key) == 1:
        if key[0] == 0:
            return b""
        expanded = bytearray(MESHTASTIC_DEFAULT_PSK)
        expanded[-1] = (expanded[-1] + key[0] - 1) & 0xFF
        return bytes(expanded)
    if len(key) < 16:
        return key.ljust(16, b"\x00")
    if 16 < len(key) < 32:
        return key.ljust(32, b"\x00")
    return key


def derive_key_from_channel_name(channel_name: str, key_base64: str) -> bytes:
    """Derive the AES key for a channel from its base64 key.

    The key is first normalised the way the firmware expands PSKs
    (:func:`normalize_psk`).  An empty channel name means "primary channel":
    the normalised key is used as-is.  Named channels derive with
    SHA256(key + name), the scheme inherited from Malla for secondary
    channels whose own PSK we never see.
    """
    try:
        key_bytes = normalize_psk(base64.b64decode(key_base64))
        if not key_bytes:
            return b""  # "no encryption" key — nothing to decrypt with
        if channel_name:
            hasher = hashlib.sha256()
            hasher.update(key_bytes)
            hasher.update(channel_name.encode("utf-8"))
            return hasher.digest()
        return key_bytes
    except Exception as exc:  # noqa: BLE001 - bad config must not kill the loop
        logger.warning("Error deriving channel key: %s", exc)
        return b""


def decrypt_packet(
    encrypted_payload: bytes, packet_id: int, sender_id: int, key: bytes
) -> bytes:
    """AES-256-CTR decrypt.  Returns ``b""`` on any failure."""
    try:
        if not encrypted_payload:
            return b""

        nonce = packet_id.to_bytes(8, "little") + sender_id.to_bytes(8, "little")
        if len(nonce) != 16:
            logger.warning("Invalid nonce length: %s", len(nonce))
            return b""

        cipher = Cipher(algorithms.AES(key), modes.CTR(nonce), backend=default_backend())
        decryptor = cipher.decryptor()
        return decryptor.update(encrypted_payload) + decryptor.finalize()
    except Exception as exc:  # noqa: BLE001
        logger.debug("Decryption failed: %s", exc)
        return b""


def try_decrypt_mesh_packet(
    mesh_packet: Any, channel_name: str = "", keys_base64: list[str] | None = None
) -> bool:
    """Try every configured key until the payload parses as a ``Data`` message.

    On success ``mesh_packet.decoded`` is populated in place.
    """
    from meshtastic import mesh_pb2, portnums_pb2

    try:
        if mesh_packet.decoded.portnum != portnums_pb2.PortNum.UNKNOWN_APP:
            return False  # already plaintext
        if not mesh_packet.encrypted:
            return False

        if not keys_base64:
            return False

        payload = mesh_packet.encrypted
        packet_id = mesh_packet.id
        sender_id = getattr(mesh_packet, "from")  # `from` is a Python keyword

        for key_index, key_base64 in enumerate(keys_base64):
            key = derive_key_from_channel_name(channel_name, key_base64)
            if not key:
                continue  # "no encryption" key: AES would only throw
            decrypted = decrypt_packet(payload, packet_id, sender_id, key)
            if not decrypted:
                continue

            try:
                decoded = mesh_pb2.Data()
                decoded.ParseFromString(decrypted)
            except Exception:  # noqa: BLE001 - wrong key produces garbage
                continue

            if decoded.portnum == portnums_pb2.PortNum.UNKNOWN_APP:
                continue

            mesh_packet.decoded.CopyFrom(decoded)
            logger.debug(
                "Decrypted packet %s with key %s/%s: %s",
                packet_id,
                key_index + 1,
                len(keys_base64),
                portnums_pb2.PortNum.Name(decoded.portnum),
            )
            return True

        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("Error during decryption: %s", exc)
        return False
