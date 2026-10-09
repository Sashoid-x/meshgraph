"""Packet routes for the replay animation (``graph.packet_routes``).

The endpoint feeds two stories: one mesh packet fanning out to several
gateways, and traceroute walks node by node.  Both must respect the time
window, the cap and the "direct reception only" rule — a relayed hop is not
an edge of the rssi graph.
"""

from __future__ import annotations

import time

from meshgraph import graph, store

from .conftest import make_packet
from .test_graph import _route_payload

A, B, C = 0x1001, 0x1002, 0x1003
GATEWAY_1, GATEWAY_2, GATEWAY_3 = 0x2001, 0x2002, 0x2003


def _fan(settings, packet_id=77, ts=None, gateways=(GATEWAY_1, GATEWAY_2)):
    """Insert the same packet as heard by each gateway (direct reception)."""
    for gateway in gateways:
        store.insert_packet(
            settings.db_file,
            make_packet(
                timestamp=ts if ts is not None else time.time(),
                from_node_id=A,
                portnum_name="TEXT_MESSAGE_APP",
                gateway_node_id=gateway,
                hop_start=3,
                hop_limit=3,
                mesh_packet_id=packet_id,
                rssi=-80,
                snr=5.0,
            ),
        )


def test_fan_route_lists_every_gateway(settings):
    now = time.time()
    _fan(settings, ts=now)
    payload = graph.packet_routes(settings, minutes=30)
    assert payload["routes"], "same packet on two gateways is a route"
    route = payload["routes"][0]
    assert route["kind"] == "fan"
    assert route["sender"] == A
    assert {leg["to"] for leg in route["legs"]} == {GATEWAY_1, GATEWAY_2}
    assert all(leg["rssi"] is not None for leg in route["legs"])


def test_single_gateway_packet_is_not_a_route(settings):
    _fan(settings, gateways=(GATEWAY_1,))
    assert graph.packet_routes(settings, minutes=30)["routes"] == []


def test_relayed_reception_does_not_fan(settings):
    # hop_limit < hop_start means the packet was forwarded: that gateway
    # never heard the sender directly, so no edge exists to animate.
    store.insert_packet(
        settings.db_file,
        make_packet(
            from_node_id=A,
            portnum_name="TEXT_MESSAGE_APP",
            gateway_node_id=GATEWAY_1,
            hop_start=3,
            hop_limit=3,
            mesh_packet_id=9,
        ),
    )
    store.insert_packet(
        settings.db_file,
        make_packet(
            from_node_id=A,
            portnum_name="TEXT_MESSAGE_APP",
            gateway_node_id=GATEWAY_2,
            hop_start=3,
            hop_limit=2,
            mesh_packet_id=9,
        ),
    )
    assert graph.packet_routes(settings, minutes=30)["routes"] == []


def test_window_and_limit(settings):
    old_ts = time.time() - 3 * 3600
    _fan(settings, packet_id=1, ts=old_ts)
    assert graph.packet_routes(settings, minutes=30)["routes"] == []

    # Five fresh fans, limit 3 -> the three freshest survive, oldest first.
    for offset in range(5):
        _fan(
            settings,
            packet_id=100 + offset,
            ts=time.time() - (5 - offset) * 60,
            gateways=(GATEWAY_1, GATEWAY_2, GATEWAY_3),
        )
    payload = graph.packet_routes(settings, minutes=60, limit=3)
    assert len(payload["routes"]) == 3
    kept = [route["packet_id"] for route in payload["routes"]]
    assert kept == [102, 103, 104]
    assert payload["minutes"] == 60


def test_traceroute_route_walks_the_chain(settings):
    now = time.time()
    store.insert_packet(
        settings.db_file,
        make_packet(
            timestamp=now,
            from_node_id=A,
            to_node_id=C,
            portnum_name="TRACEROUTE_APP",
            gateway_node_id=GATEWAY_1,
            hop_start=3,
            hop_limit=3,
            mesh_packet_id=55,
            raw_payload=_route_payload(route=[B], snr_towards=[40, 40]),
        ),
    )
    payload = graph.packet_routes(settings, minutes=30)
    routes = [r for r in payload["routes"] if r["kind"] == "traceroute"]
    assert len(routes) == 1
    route = routes[0]
    # The reply walks back along the discovered path: C -> B -> A (plausible
    # SNR 40/4 = 10 dB on both legs); the dot starts where the packet starts.
    assert route["sender"] == C
    assert [leg["to"] for leg in route["legs"]] == [B, A]
    assert all(leg["snr"] == 10.0 for leg in route["legs"])


def test_traceroute_route_keeps_chain_but_drops_fake_snr(settings):
    # snr_towards=[0] is the injected (MQTT) measurement: the leg stays so
    # the walk does not break, the colour signal goes away.
    store.insert_packet(
        settings.db_file,
        make_packet(
            from_node_id=A,
            to_node_id=B,
            portnum_name="TRACEROUTE_APP",
            gateway_node_id=GATEWAY_1,
            hop_start=3,
            hop_limit=3,
            raw_payload=_route_payload(route=[], snr_towards=[0]),
        ),
    )
    routes = graph.packet_routes(settings, minutes=30)["routes"]
    assert len(routes) == 1
    assert [leg["to"] for leg in routes[0]["legs"]] == [B]
    assert routes[0]["legs"][0]["snr"] is None


def test_mixed_sources_are_capped_and_ordered(settings):
    now = time.time()
    for offset in range(3):
        _fan(settings, packet_id=200 + offset, ts=now - (3 - offset) * 60)
    store.insert_packet(
        settings.db_file,
        make_packet(
            timestamp=now,
            from_node_id=A,
            to_node_id=B,
            portnum_name="TRACEROUTE_APP",
            gateway_node_id=GATEWAY_1,
            hop_start=3,
            hop_limit=3,
            raw_payload=_route_payload(route=[], snr_towards=[40]),
        ),
    )
    payload = graph.packet_routes(settings, minutes=60, limit=3)
    assert len(payload["routes"]) == 3  # cap holds across both sources
    stamps = [route["ts"] for route in payload["routes"]]
    assert stamps == sorted(stamps)  # chronological replay order
