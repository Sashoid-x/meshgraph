"""The demo generator must feed both graph modes end to end."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from meshgraph import chat, graph, store
from meshgraph.config import DEFAULT_CHANNEL_KEY
from meshgraph.decoder import decode_message

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "demo_traffic.py"


@pytest.fixture(scope="module")
def demo():
    spec = importlib.util.spec_from_file_location("meshgraph_demo_traffic", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def decoded_packets(demo, settings):
    """Publish path minus MQTT: decode the scene and store every packet."""
    scene = demo.Scene(region="US", channel="LongFast")
    packets = []
    for topic, payload in demo.build_messages(scene):
        packet = decode_message(topic, payload, keys=[DEFAULT_CHANNEL_KEY])
        assert packet is not None, topic
        assert packet.error is None, packet.error
        packets.append(packet)
        store.insert_packet(settings.db_file, packet)
    graph.invalidate_cache()
    yield packets
    graph.invalidate_cache()


def test_scene_covers_both_modes(decoded_packets):
    portnums = {p.portnum_name for p in decoded_packets}
    assert "TRACEROUTE_APP" in portnums
    assert "NODEINFO_APP" in portnums
    assert "POSITION_APP" in portnums
    assert all(p.processed for p in decoded_packets), [
        p.error for p in decoded_packets if not p.processed
    ]
    # The one AES-encrypted packet must decrypt with the configured key.
    assert any(p.decrypted for p in decoded_packets)


def test_traceroute_mode_builds_full_mesh(decoded_packets, settings):
    result = graph.build_graph(settings, mode="traceroute", use_cache=False)
    assert result["stats"]["nodes"] == 5
    assert result["stats"]["links"] == 6
    assert result["stats"]["gateways"] == 2
    names = {n["name"] for n in result["nodes"]}
    assert {"Mayak", "Baza", "Kuryer"} <= names
    # Every link carries a plausible average SNR.
    assert all(0 < link["avg_snr"] < 20 for link in result["links"])


def test_rssi_mode_ignores_relayed_packets(decoded_packets, settings):
    result = graph.build_graph(settings, mode="rssi", use_cache=False)
    stats = result["stats"]
    assert stats["receptions_relayed"] >= 1  # the deliberately relayed packet
    assert stats["receptions_plausible"] == stats["receptions_analyzed"]
    assert stats["links"] >= 4
    # Relay or not, the nodes the demo names are all visible.
    assert {n["name"] for n in result["nodes"]} >= {"Mayak", "Baza", "Retranslator"}


def test_positions_reach_the_graph(decoded_packets, settings):
    result = graph.build_graph(settings, mode="rssi", use_cache=False)
    located = [n for n in result["nodes"] if n.get("location")]
    assert len(located) >= 3


def test_chat_shows_dialogue_with_quote_and_reaction(decoded_packets, settings):
    """The demo scene exercises the whole chat path: decode → store → API."""
    payload = chat.build_chat(settings, hours=1)

    by_text = {m["text"]: m for m in payload["messages"]}
    host = by_text["Всем привет! Как там погода?"]
    assert host["name"] == "Shturman"

    # The answer carries a quote with the original text and its author.
    reply = by_text["Дубак, −15 и ветер"]
    assert reply["name"] == "Kuryer"
    assert reply["reply_to"]["text"] == "Всем привет! Как там погода?"
    assert reply["reply_to"]["name"] == "Shturman"

    # The emoji answer became a pill on the target, not a message of its own.
    assert "👍" not in by_text
    assert host["reactions"] == [{"emoji": "👍", "count": 1, "names": ["Baza"]}]
