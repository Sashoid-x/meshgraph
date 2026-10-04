"""Shared fixtures: temp settings, temp database, synthetic packet builders."""

from __future__ import annotations

import time

import pytest

from meshgraph import store
from meshgraph.config import Settings, SettingsStore
from meshgraph.decoder import DecodedPacket


@pytest.fixture
def db_file(tmp_path) -> str:
    return str(tmp_path / "graph.db")


@pytest.fixture
def settings(db_file, tmp_path) -> Settings:
    value = Settings(
        db_file=db_file,
        mqtt_broker_address="127.0.0.1",
        web_host="127.0.0.1",
        web_port=0,
    )
    store.ensure_ready(value)
    return value


@pytest.fixture
def settings_store(tmp_path) -> SettingsStore:
    return SettingsStore(path=tmp_path / "config.yaml")


def make_packet(
    *,
    timestamp: float | None = None,
    from_node_id: int = 1,
    to_node_id: int = 0xFFFFFFFF,
    portnum_name: str = "TRACEROUTE_APP",
    gateway_node_id: int | None = None,
    gateway_id: str | None = None,
    channel_id: str | None = "LongFast",
    rssi: int | None = -90,
    snr: float | None = 6.5,
    hop_limit: int | None = None,
    hop_start: int | None = None,
    raw_payload: bytes = b"",
    processed: bool = True,
    mesh_packet_id: int | None = None,
    reply_id: int | None = None,
    emoji: int | None = None,
    node_info: dict | None = None,
) -> DecodedPacket:
    """Build a DecodedPacket the way the MQTT decoder would emit it."""
    if gateway_id is None and gateway_node_id is not None:
        gateway_id = f"!{gateway_node_id & 0xFFFFFFFF:08x}"
    return DecodedPacket(
        timestamp=timestamp if timestamp is not None else time.time(),
        topic=f"msh/US/2/e/LongFast/{gateway_id or '!00000001'}",
        message_type="e",
        from_node_id=from_node_id,
        to_node_id=to_node_id,
        portnum_name=portnum_name,
        gateway_id=gateway_id,
        gateway_node_id=gateway_node_id if gateway_node_id is not None else 1,
        channel_id=channel_id,
        rssi=rssi,
        snr=snr,
        hop_limit=hop_limit,
        hop_start=hop_start,
        mesh_packet_id=mesh_packet_id,
        reply_id=reply_id,
        emoji=emoji,
        payload_length=len(raw_payload),
        raw_payload=raw_payload,
        processed=processed,
        node_info=node_info,
    )
