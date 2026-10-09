"""Packet routes for the packet animation (``graph.packet_routes``/``packet_flow``).

Every reception is a movement: a direct packet flies sender → gateway — one
movement per hearing gateway, duplicates included (the chat folds them, the
animation flies them), a traceroute crawls its hop chain.  Both feeds must
respect the window, the cap and chronological order; self-receptions (the
gateway originated the packet) have no path to draw and are skipped.
"""

from __future__ import annotations

import time

from meshgraph import graph, store

from .conftest import make_packet
from .test_graph import _route_payload

A, B, C = 0x1001, 0x1002, 0x1003
GATEWAY_1, GATEWAY_2, GATEWAY_3 = 0x2001, 0x2002, 0x2003


def _insert(
    settings,
    *,
    packet_id=77,
    ts=None,
    from_node=A,
    gateways=(GATEWAY_1, GATEWAY_2),
    **kwargs,
):
    """Insert the same packet as heard by each of the given gateways."""
    for gateway in gateways:
        store.insert_packet(
            settings.db_file,
            make_packet(
                timestamp=ts if ts is not None else time.time(),
                from_node_id=from_node,
                portnum_name="TEXT_MESSAGE_APP",
                gateway_node_id=gateway,
                hop_start=3,
                hop_limit=3,
                mesh_packet_id=packet_id,
                rssi=-80,
                snr=5.0,
                **kwargs,
            ),
        )


def test_each_hearing_gateway_yields_its_own_movement(settings):
    _insert(settings, packet_id=77, gateways=(GATEWAY_1, GATEWAY_2))
    routes = graph.packet_routes(settings, minutes=30)["routes"]
    assert len(routes) == 2, "one movement per reception — duplicates included"
    assert {route["kind"] for route in routes} == {"direct"}
    assert {route["sender"] for route in routes} == {A}
    # Same packet id ties the duplicates together (one colour everywhere).
    assert {route["packet_id"] for route in routes} == {77}
    assert {route["legs"][0]["to"] for route in routes} == {GATEWAY_1, GATEWAY_2}
    assert all(route["legs"][0]["rssi"] is not None for route in routes)


def test_single_gateway_packet_moves_too(settings):
    _insert(settings, gateways=(GATEWAY_1,))
    routes = graph.packet_routes(settings, minutes=30)["routes"]
    assert len(routes) == 1
    assert routes[0]["legs"][0]["to"] == GATEWAY_1


def test_relayed_reception_still_moves(settings):
    # hop_limit < hop_start means a relay forwarded the packet: the straight
    # line sender → gateway is still the movement the user watched.
    _insert(settings, packet_id=9, gateways=(GATEWAY_1,))
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
    routes = graph.packet_routes(settings, minutes=30)["routes"]
    assert len(routes) == 2


def test_self_reception_is_skipped(settings):
    # The gateway sent this packet itself: it left the node, we cannot draw
    # where it went.
    _insert(settings, from_node=GATEWAY_1, gateways=(GATEWAY_1,))
    assert graph.packet_routes(settings, minutes=30)["routes"] == []


def test_window_and_limit(settings):
    old_ts = time.time() - 3 * 3600
    _insert(settings, packet_id=1, ts=old_ts, gateways=(GATEWAY_1,))
    assert graph.packet_routes(settings, minutes=30)["routes"] == []

    # Five fresh packets, limit 3 -> the three freshest survive, oldest first.
    for offset in range(5):
        _insert(
            settings,
            packet_id=100 + offset,
            ts=time.time() - (5 - offset) * 60,
            gateways=(GATEWAY_1,),
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
    # the walk does not break, the measurement goes away.
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
        _insert(
            settings,
            packet_id=200 + offset,
            ts=now - (3 - offset) * 60,
            gateways=(GATEWAY_1,),
        )
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


def test_packet_flow_returns_only_new_arrivals(settings):
    now = time.time()
    _insert(settings, packet_id=1, ts=now - 3600, gateways=(GATEWAY_1,))
    _insert(settings, packet_id=2, ts=now - 5, gateways=(GATEWAY_1,))

    fresh = graph.packet_flow(settings, since=now - 60)["routes"]
    assert [route["packet_id"] for route in fresh] == [2]

    everything = graph.packet_flow(settings, since=now - 7200)["routes"]
    assert [route["packet_id"] for route in everything] == [1, 2]


def test_packet_flow_falls_back_to_the_backfill(settings):
    now = time.time()
    _insert(settings, packet_id=5, ts=now - 60, gateways=(GATEWAY_1,))

    # Garbage, zero and future timestamps all mean "start from the backfill
    # window" — never a full scan, never a client clock.
    for since in ("banana", 0, now + 3600, None):
        payload = graph.packet_flow(settings, since=since)
        assert payload["since"] <= payload["generated_at"]
        assert now - graph.LIVE_BACKFILL_SECONDS - 5 <= payload["since"] <= now
        assert [route["packet_id"] for route in payload["routes"]] == [5]
