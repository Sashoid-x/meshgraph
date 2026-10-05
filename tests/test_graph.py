"""Graph construction in both modes."""

from __future__ import annotations

import json
import threading
import time

import pytest
from meshtastic import mesh_pb2

from meshgraph import graph, store
from meshgraph.decoder import DecodedPacket

from .conftest import make_packet

A, B, C, D = 0x1001, 0x1002, 0x1003, 0x1004
GATEWAY = 0x2001
X, Y = 0x3001, 0x3002


def _route_payload(route=(), snr_towards=(), route_back=(), snr_back=()) -> bytes:
    msg = mesh_pb2.RouteDiscovery()
    msg.route.extend(route)
    msg.snr_towards.extend(snr_towards)
    msg.route_back.extend(route_back)
    msg.snr_back.extend(snr_back)
    return msg.SerializeToString()


def add_node(settings, node_id, name, latitude=None, longitude=None, role="CLIENT"):
    packet = DecodedPacket(
        timestamp=make_packet().timestamp,
        topic="msh/US/2/e/LongFast/!00000001",
        from_node_id=node_id,
        portnum_name="NODEINFO_APP",
        node_info={
            "node_id": node_id,
            "hex_id": f"!{node_id:08x}",
            "long_name": name,
            "short_name": name[:4],
            "hw_model": "HELTEC_V3",
            "role": role,
        },
    )
    if latitude is not None:
        packet.position = {
            "latitude": latitude,
            "longitude": longitude,
            "altitude": 10,
            "timestamp": packet.timestamp,
        }
    store.insert_packet(settings.db_file, packet)


@pytest.fixture(autouse=True)
def _fresh_cache():
    graph.invalidate_cache()
    yield
    graph.invalidate_cache()


@pytest.fixture
def seeded(settings):
    """A three-hop traceroute A→B→C→D plus a named, located roster."""
    add_node(settings, A, "Alpha", latitude=55.75, longitude=37.61)
    add_node(settings, B, "Bravo", latitude=55.76, longitude=37.62)
    add_node(settings, C, "Charlie", latitude=55.77, longitude=37.63)
    add_node(settings, D, "Delta", latitude=55.78, longitude=37.64)
    add_node(settings, GATEWAY, "Gateway", role="ROUTER")

    store.insert_packet(
        settings.db_file,
        make_packet(
            from_node_id=A,
            to_node_id=D,
            gateway_node_id=GATEWAY,
            portnum_name="TRACEROUTE_APP",
            raw_payload=_route_payload(
                route=[B, C], snr_towards=[40, 44, 48]
            ),  # 10, 11, 12 dB
        ),
    )
    return settings


# ---------------------------------------------------------------------------
# Traceroute mode
# ---------------------------------------------------------------------------

def test_traceroute_mode_builds_the_chain(seeded):
    payload = graph.build_graph(seeded, mode="traceroute", hours=24, use_cache=False)

    assert payload["mode"] == "traceroute"
    assert {n["id"] for n in payload["nodes"]} == {A, B, C, D}
    assert len(payload["links"]) == 3
    assert payload["stats"]["packets_with_rf_hops"] == 1
    assert payload["stats"]["total_rf_hops"] == 3


def test_traceroute_links_carry_average_snr(seeded):
    payload = graph.build_graph(seeded, mode="traceroute", use_cache=False)
    links = {(l["source"], l["target"]): l for l in payload["links"]}

    assert links[(A, B)]["avg_snr"] == 10.0
    assert links[(B, C)]["avg_snr"] == 11.0
    assert links[(C, D)]["avg_snr"] == 12.0
    assert all(l["type"] == "direct" for l in payload["links"])
    assert all(1 <= l["strength"] <= 10 for l in payload["links"])


def test_node_names_and_locations_resolve(seeded):
    payload = graph.build_graph(seeded, mode="traceroute", use_cache=False)
    by_id = {n["id"]: n for n in payload["nodes"]}

    assert by_id[A]["name"] == "Alpha"
    assert by_id[D]["name"] == "Delta"
    assert by_id[A]["location"]["latitude"] == pytest.approx(55.75)
    assert by_id[A]["hex_id"] == f"!{A:08x}"
    # GATEWAY uplinked the packet, so it is flagged as a gateway when present.
    assert by_id[A]["is_gateway"] is False


def test_unknown_nodes_fall_back_to_hex_id(settings):
    store.insert_packet(
        settings.db_file,
        make_packet(
            from_node_id=0x9999,
            to_node_id=0x8888,
            gateway_node_id=GATEWAY,
            portnum_name="TRACEROUTE_APP",
            raw_payload=_route_payload(route=[], snr_towards=[32]),  # 8 dB
        ),
    )
    payload = graph.build_graph(settings, mode="traceroute", use_cache=False)
    names = {n["name"] for n in payload["nodes"]}
    assert names == {"!00009999", "!00008888"}


def test_min_snr_filters_weak_links_and_orphan_nodes(seeded):
    payload = graph.build_graph(
        seeded, mode="traceroute", min_snr=11, use_cache=False
    )
    pairs = {(l["source"], l["target"]) for l in payload["links"]}

    assert (A, B) not in pairs  # 10 dB is below the threshold
    assert (B, C) in pairs
    assert (C, D) in pairs
    # A dropped out of the node list because all its links were filtered.
    assert A not in {n["id"] for n in payload["nodes"]}


def test_zero_snr_hops_are_dropped(settings):
    # snr == 0 marks an MQTT/UDP injected link rather than a real RF hop.
    store.insert_packet(
        settings.db_file,
        make_packet(
            from_node_id=A,
            to_node_id=B,
            portnum_name="TRACEROUTE_APP",
            raw_payload=_route_payload(route=[], snr_towards=[0]),
        ),
    )
    payload = graph.build_graph(settings, mode="traceroute", use_cache=False)
    assert payload["links"] == []
    assert payload["stats"]["links_filtered_due_to_snr_0"] == 1


def test_broadcast_endpoints_are_dropped(settings):
    store.insert_packet(
        settings.db_file,
        make_packet(
            from_node_id=A,
            to_node_id=0xFFFFFFFF,
            portnum_name="TRACEROUTE_APP",
            raw_payload=_route_payload(route=[], snr_towards=[40]),
        ),
    )
    payload = graph.build_graph(settings, mode="traceroute", use_cache=False)
    assert payload["links"] == []
    assert payload["nodes"] == []


def test_indirect_connections_add_scoped_endpoints(seeded):
    without = graph.build_graph(
        seeded, mode="traceroute", include_indirect=False, use_cache=False
    )
    assert without["indirect_connections"] == []

    with_indirect = graph.build_graph(
        seeded, mode="traceroute", include_indirect=True, use_cache=False
    )
    assert len(with_indirect["indirect_connections"]) == 1
    conn = with_indirect["indirect_connections"][0]
    assert {conn["source"], conn["target"]} == {A, D}
    assert conn["hop_count"] == 3
    assert conn["type"] == "indirect"


def test_round_trip_does_not_link_node_to_itself(settings):
    # Shape of a real round-trip packet (from D to A with a return path): the
    # parsed hops start and end at the originator, so the indirect key is (A, A).
    store.insert_packet(
        settings.db_file,
        make_packet(
            from_node_id=D,
            to_node_id=A,
            portnum_name="TRACEROUTE_APP",
            raw_payload=_route_payload(
                route=[C], snr_towards=[28, 24], route_back=[B], snr_back=[20, 16]
            ),
        ),
    )
    payload = graph.build_graph(
        settings, mode="traceroute", include_indirect=True, use_cache=False
    )

    assert payload["indirect_connections"] == []
    # The direct hops of the trip are still drawn.
    assert len(payload["links"]) == 4


def test_indirect_not_duplicated_when_direct_link_exists(settings):
    import time

    # Newest first: the direct A↔B hop must already be in the table when the
    # multi-hop path is processed, otherwise the indirect line wins the race.
    store.insert_packet(
        settings.db_file,
        make_packet(
            from_node_id=A,
            to_node_id=B,
            timestamp=time.time(),
            portnum_name="TRACEROUTE_APP",
            raw_payload=_route_payload(route=[], snr_towards=[40]),
        ),
    )
    store.insert_packet(
        settings.db_file,
        make_packet(
            from_node_id=A,
            to_node_id=B,
            timestamp=time.time() - 60,
            portnum_name="TRACEROUTE_APP",
            raw_payload=_route_payload(route=[C], snr_towards=[40, 40]),
        ),
    )
    payload = graph.build_graph(
        settings, mode="traceroute", include_indirect=True, use_cache=False
    )
    pairs = {frozenset((l["source"], l["target"])) for l in payload["links"]}
    assert frozenset((A, B)) in pairs
    # A↔B already exists directly, so no indirect line for the same endpoints.
    assert payload["indirect_connections"] == []


def test_channel_filter_narrows_results(seeded):
    store.insert_packet(
        seeded.db_file,
        make_packet(
            from_node_id=B,
            to_node_id=D,
            channel_id="Private",
            portnum_name="TRACEROUTE_APP",
            raw_payload=_route_payload(route=[], snr_towards=[40]),
        ),
    )
    all_channels = graph.build_graph(seeded, mode="traceroute", use_cache=False)
    private = graph.build_graph(
        seeded, mode="traceroute", channel="Private", use_cache=False
    )
    assert len(all_channels["links"]) == 4
    assert len(private["links"]) == 1
    assert private["filters"]["channel"] == "Private"


# ---------------------------------------------------------------------------
# RSSI mode
# ---------------------------------------------------------------------------

def test_rssi_mode_links_gateway_to_direct_transmitters(settings):
    for node, snr, rssi in ((X, 7.5, -80), (Y, -3.0, -110)):
        store.insert_packet(
            settings.db_file,
            make_packet(
                from_node_id=node,
                gateway_node_id=GATEWAY,
                hop_limit=3,
                hop_start=3,  # 0 remaining hops = heard directly
                snr=snr,
                rssi=rssi,
                portnum_name="TELEMETRY_APP",
            ),
        )

    payload = graph.build_graph(settings, mode="rssi", hours=24, use_cache=False)

    assert payload["mode"] == "rssi"
    pairs = {frozenset((l["source"], l["target"])) for l in payload["links"]}
    assert frozenset((GATEWAY, X)) in pairs
    assert frozenset((GATEWAY, Y)) in pairs
    assert {n["id"] for n in payload["nodes"]} == {GATEWAY, X, Y}

    gateway_node = next(n for n in payload["nodes"] if n["id"] == GATEWAY)
    assert gateway_node["is_gateway"] is True
    assert next(n for n in payload["nodes"] if n["id"] == X)["is_gateway"] is False


def test_rssi_mode_ignores_relayed_packets(settings):
    store.insert_packet(
        settings.db_file,
        make_packet(
            from_node_id=Y,
            gateway_node_id=GATEWAY,
            hop_limit=2,
            hop_start=3,  # one relay in between → not a direct reception
            portnum_name="TELEMETRY_APP",
        ),
    )
    payload = graph.build_graph(settings, mode="rssi", use_cache=False)
    assert payload["links"] == []
    assert payload["nodes"] == []
    # Excluded in SQL (hop budget not intact) but reported so the UI can explain
    # why the RSSI graph looks sparse.
    assert payload["stats"]["receptions_analyzed"] == 0
    assert payload["stats"]["receptions_relayed"] == 1


def test_rssi_mode_ignores_gateway_uplink_of_itself(settings):
    store.insert_packet(
        settings.db_file,
        make_packet(
            from_node_id=GATEWAY,
            gateway_node_id=GATEWAY,
            hop_limit=3,
            hop_start=3,
            portnum_name="NODEINFO_APP",
        ),
    )
    payload = graph.build_graph(settings, mode="rssi", use_cache=False)
    assert payload["links"] == []


def test_rssi_mode_drops_implausible_signal_values(settings):
    # rssi == 0 is the "not provided" sentinel and snr 40 dB is off the scale.
    store.insert_packet(
        settings.db_file,
        make_packet(
            from_node_id=X,
            gateway_node_id=GATEWAY,
            hop_limit=3,
            hop_start=3,
            rssi=0,
            snr=40.0,
        ),
    )
    payload = graph.build_graph(settings, mode="rssi", use_cache=False)
    assert payload["links"] == []
    assert payload["stats"]["links_filtered"] == 1


def test_rssi_mode_aggregates_repeated_receptions(settings):
    for _ in range(3):
        store.insert_packet(
            settings.db_file,
            make_packet(
                from_node_id=X,
                gateway_node_id=GATEWAY,
                hop_limit=3,
                hop_start=3,
                snr=8.0,
                rssi=-75,
            ),
        )
    payload = graph.build_graph(settings, mode="rssi", use_cache=False)
    assert len(payload["links"]) == 1
    link = payload["links"][0]
    assert link["packet_count"] == 3
    assert link["avg_snr"] == 8.0
    assert link["avg_rssi"] == -75.0

    gateway_node = next(n for n in payload["nodes"] if n["id"] == GATEWAY)
    assert gateway_node["packet_count"] == 3


def test_duplicate_reception_does_not_inflate_strength(settings):
    """The same reception redelivered by the broker must not double the weight."""
    for _ in range(2):  # a redelivery inside the dedup window
        store.insert_packet(
            settings.db_file,
            make_packet(
                from_node_id=X,
                gateway_node_id=GATEWAY,
                hop_limit=3,
                hop_start=3,
                snr=7.5,
                rssi=-80,
                portnum_name="TELEMETRY_APP",
                mesh_packet_id=777,
            ),
        )

    payload = graph.build_graph(settings, mode="rssi", hours=24, use_cache=False)
    link = payload["links"][0]

    assert link["packet_count"] == 1
    assert link["strength"] == graph._strength(7.5, -80, 1)


def test_rssi_mode_applies_min_snr(settings):
    store.insert_packet(
        settings.db_file,
        make_packet(
            from_node_id=X,
            gateway_node_id=GATEWAY,
            hop_limit=3,
            hop_start=3,
            snr=-25.0,
        ),
    )
    payload = graph.build_graph(settings, mode="rssi", min_snr=-10, use_cache=False)
    assert payload["links"] == []
    assert payload["stats"]["links_filtered"] == 1


# ---------------------------------------------------------------------------
# Both modes
# ---------------------------------------------------------------------------

def test_both_modes_cover_different_node_sets(settings):
    """RSSI mode must see nodes that never run traceroutes."""
    store.insert_packet(
        settings.db_file,
        make_packet(
            from_node_id=A,
            to_node_id=B,
            gateway_node_id=GATEWAY,
            portnum_name="TRACEROUTE_APP",
            raw_payload=_route_payload(route=[], snr_towards=[40]),
        ),
    )
    store.insert_packet(
        settings.db_file,
        make_packet(
            from_node_id=Y,  # never appears in any traceroute
            gateway_node_id=GATEWAY,
            hop_limit=3,
            hop_start=3,
        ),
    )

    traceroute = graph.build_graph(settings, mode="traceroute", use_cache=False)
    rssi = graph.build_graph(settings, mode="rssi", use_cache=False)

    assert {n["id"] for n in traceroute["nodes"]} == {A, B}
    assert Y in {n["id"] for n in rssi["nodes"]}


# ---------------------------------------------------------------------------
# Combined mode
# ---------------------------------------------------------------------------

@pytest.fixture
def seeded_both(seeded):
    """The traceroute chain plus two direct receptions of the same nodes."""
    for node, snr, rssi in ((X, 7.5, -80), (B, -3.0, -110)):
        store.insert_packet(
            seeded.db_file,
            make_packet(
                from_node_id=node,
                gateway_node_id=GATEWAY,
                hop_limit=3,
                hop_start=3,  # heard directly
                snr=snr,
                rssi=rssi,
                portnum_name="TELEMETRY_APP",
            ),
        )
    return seeded


def test_combined_mode_unions_both_graphs(seeded_both):
    payload = graph.build_graph(
        seeded_both, mode="combined", hours=24, use_cache=False
    )

    assert payload["mode"] == "combined"
    # Traceroute chain A–B–C–D plus the reception-only pair GATEWAY–X.
    assert {n["id"] for n in payload["nodes"]} == {A, B, C, D, GATEWAY, X}
    pairs = {frozenset((l["source"], l["target"])) for l in payload["links"]}
    assert {frozenset(p) for p in ((A, B), (B, C), (C, D))} <= pairs
    assert frozenset((GATEWAY, X)) in pairs
    assert frozenset((GATEWAY, B)) in pairs

    assert payload["stats"]["nodes"] == 6
    assert payload["stats"]["gateways"] == 1
    assert payload["stats"]["traceroute"]["mode"] == "traceroute"
    assert payload["stats"]["rssi"]["mode"] == "rssi"
    # The sidebar values are the rssi-mode numbers, hoisted for convenience.
    assert (
        payload["stats"]["packets_analyzed"]
        == payload["stats"]["rssi"]["receptions_analyzed"]
    )
    assert (
        payload["stats"]["receptions_relayed"]
        == payload["stats"]["rssi"]["receptions_relayed"]
    )


def test_combined_node_degree_is_the_union(seeded_both):
    payload = graph.build_graph(seeded_both, mode="combined", use_cache=False)
    by_id = {n["id"]: n for n in payload["nodes"]}

    # B: traceroute neighbours A and C, plus the reception neighbour GATEWAY.
    assert by_id[B]["connections"] == 3
    assert by_id[X]["connections"] == 1
    # Reception-only node keeps its RSSI; traceroute nodes keep their SNR.
    assert by_id[X]["avg_rssi"] == -80.0
    assert by_id[B]["avg_rssi"] == -110.0
    assert by_id[A]["avg_snr"] == 10.0
    assert by_id[B]["is_gateway"] is False
    assert by_id[GATEWAY]["is_gateway"] is True


def test_combined_link_seen_by_both_modes_merges_metrics(settings):
    # The same GATEWAY↔A pair: once as a traceroute hop, once as a reception.
    store.insert_packet(
        settings.db_file,
        make_packet(
            from_node_id=GATEWAY,
            to_node_id=A,
            portnum_name="TRACEROUTE_APP",
            raw_payload=_route_payload(route=[], snr_towards=[40]),  # 10 dB
        ),
    )
    store.insert_packet(
        settings.db_file,
        make_packet(
            from_node_id=A,
            gateway_node_id=GATEWAY,
            hop_limit=3,
            hop_start=3,
            snr=5.0,
            rssi=-95,
            portnum_name="TELEMETRY_APP",
        ),
    )

    payload = graph.build_graph(settings, mode="combined", use_cache=False)
    link = next(
        l
        for l in payload["links"]
        if {l["source"], l["target"]} == {GATEWAY, A}
    )

    assert link["modes"] == ["traceroute", "rssi"]
    assert link["avg_snr"] == 10.0  # the traceroute measurement wins
    assert link["avg_rssi"] == -95.0  # only receptions know RSSI
    assert 1 <= link["strength"] <= 10
    assert len(payload["links"]) == 1


def test_combined_keeps_indirect_connections(seeded):
    payload = graph.build_graph(
        seeded, mode="combined", include_indirect=True, use_cache=False
    )
    assert len(payload["indirect_connections"]) == 1
    assert payload["stats"]["indirect"] == 1


def test_unknown_mode_falls_back_to_traceroute(seeded):
    payload = graph.build_graph(seeded, mode="nonsense", use_cache=False)
    assert payload["mode"] == "traceroute"


def test_hours_are_sanitised(seeded):
    payload = graph.build_graph(seeded, mode="traceroute", hours=99999, use_cache=False)
    assert payload["filters"]["hours"] == 24


def test_cache_returns_identical_payload(seeded):
    first = graph.build_graph(seeded, mode="traceroute", use_cache=True)
    second = graph.build_graph(seeded, mode="traceroute", use_cache=True)
    assert first is second

    graph.invalidate_cache()
    third = graph.build_graph(seeded, mode="traceroute", use_cache=True)
    assert third is not first
    assert third["nodes"] == first["nodes"]


def test_parallel_builds_compute_once(settings, monkeypatch):
    """Single-flight: four concurrent misses on one key share one rebuild."""
    calls = []
    original = graph._build_traceroute

    def counting(*args, **kwargs):
        calls.append(1)
        time.sleep(0.3)  # hold the flight open so the others have to queue
        return original(*args, **kwargs)

    monkeypatch.setattr(graph, "_build_traceroute", counting)
    results = []

    def worker():
        results.append(graph.build_graph(settings, mode="traceroute", use_cache=True))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(calls) == 1
    assert all(payload is results[0] for payload in results)


def test_insert_invalidates_cached_payload_immediately(settings):
    """New packets must show up at once, not after the 20 s TTL (G-P0-3)."""
    first = graph.build_graph(settings, mode="traceroute", use_cache=True)
    assert first["nodes"] == []

    store.insert_packet(
        settings.db_file,
        make_packet(
            from_node_id=A,
            to_node_id=0x8888,
            gateway_node_id=GATEWAY,
            portnum_name="TRACEROUTE_APP",
            raw_payload=_route_payload(route=[], snr_towards=[40]),
            mesh_packet_id=5,
        ),
    )

    second = graph.build_graph(settings, mode="traceroute", use_cache=True)
    assert second is not first
    assert {node["id"] for node in second["nodes"]} == {A, 0x8888}

    # A redelivered duplicate stores no row — the generation must not move,
    # otherwise every retry would needlessly invalidate the cache.
    generation = store.generation()
    store.insert_packet(
        settings.db_file,
        make_packet(
            from_node_id=A,
            to_node_id=0x8888,
            gateway_node_id=GATEWAY,
            portnum_name="TRACEROUTE_APP",
            raw_payload=_route_payload(route=[], snr_towards=[40]),
            mesh_packet_id=5,  # the same packet that already sits in the table
        ),
    )
    assert store.generation() == generation

    # Unchanged data → same key → the cached object is served again.
    third = graph.build_graph(settings, mode="traceroute", use_cache=True)
    assert third is second


# ---------------------------------------------------------------------------
# Measurement honesty and telemetry (G-P1-3, G-P1-4, G-P2-1, G-P2-3)
# ---------------------------------------------------------------------------


def test_traceroute_separates_injected_zero_from_implausible_snr(settings):
    # A-B entered over MQTT/UDP (snr exactly 0), X-Y carries40 dB junk.
    store.insert_packet(
        settings.db_file,
        make_packet(
            from_node_id=A,
            to_node_id=B,
            portnum_name="TRACEROUTE_APP",
            raw_payload=_route_payload(route=[], snr_towards=[0]),
        ),
    )
    store.insert_packet(
        settings.db_file,
        make_packet(
            from_node_id=X,
            to_node_id=Y,
            portnum_name="TRACEROUTE_APP",
            raw_payload=_route_payload(route=[], snr_towards=[160]),  # 40 dB
        ),
    )

    payload = graph.build_graph(settings, mode="traceroute", use_cache=False)
    stats = payload["stats"]

    # Different reasons, different counters: nothing is lumped together.
    assert stats["links_filtered_due_to_snr_0"] == 1  # the injected "hop"
    assert stats["links_filtered_by_snr"] == 1        # the out-of-range value
    assert stats["links_found"] == 0


def test_rssi_distinguishes_injected_zero_from_unknown_snr(settings):
    # Two receptions of different pairs: one has the fake 0 dB, one no SNR.
    store.insert_packet(
        settings.db_file,
        make_packet(
            from_node_id=A,
            gateway_node_id=GATEWAY,
            snr=0.0,
            rssi=-80,
            hop_limit=3,
            hop_start=3,
            portnum_name="TELEMETRY_APP",
        ),
    )
    store.insert_packet(
        settings.db_file,
        make_packet(
            from_node_id=X,
            gateway_node_id=GATEWAY,
            snr=None,
            rssi=-70,
            hop_limit=3,
            hop_start=3,
            portnum_name="TELEMETRY_APP",
        ),
    )

    payload = graph.build_graph(settings, mode="rssi", use_cache=False)
    stats = payload["stats"]
    links = {(l["source"], l["target"]): l for l in payload["links"]}

    # Each case landed in its own counter, not a shared one.
    assert stats["receptions_snr_injected"] == 1
    assert stats["receptions_snr_unknown"] == 1
    assert stats["receptions_plausible"] == 2
    # The fake zero is not averaged in: the links keep only their RSSI.
    assert links[(A, GATEWAY)]["avg_snr"] is None
    assert links[(A, GATEWAY)]["avg_rssi"] == -80.0
    assert links[(GATEWAY, X)]["avg_snr"] is None
    assert links[(GATEWAY, X)]["avg_rssi"] == -70.0


def test_rssi_filter_reasons_do_not_mix(settings):
    # One row has no measurements at all; the other fails the min_snr filter.
    store.insert_packet(
        settings.db_file,
        make_packet(
            from_node_id=A,
            gateway_node_id=GATEWAY,
            snr=None,
            rssi=0,  # 0 is the "not provided" sentinel
            hop_limit=3,
            hop_start=3,
            portnum_name="TELEMETRY_APP",
        ),
    )
    store.insert_packet(
        settings.db_file,
        make_packet(
            from_node_id=X,
            gateway_node_id=GATEWAY,
            snr=3.0,
            rssi=-70,
            hop_limit=3,
            hop_start=3,
            portnum_name="TELEMETRY_APP",
        ),
    )

    payload = graph.build_graph(settings, mode="rssi", min_snr=5, use_cache=False)
    stats = payload["stats"]

    assert stats["links_filtered_no_signal"] == 1
    assert stats["links_filtered_below_min_snr"] == 1
    assert stats["links_filtered"] == 2  # итог остаётся суммой причин
    assert payload["links"] == []


def test_bad_snr_row_is_counted_not_fatal(settings):
    # A string in the REAL column makes is_plausible_snr raise: without the
    # row guard the whole build would die on the first row (G-P1-4).
    store.insert_packet(
        settings.db_file,
        make_packet(
            from_node_id=A,
            gateway_node_id=GATEWAY,
            snr="junk",
            rssi=-80,
            hop_limit=3,
            hop_start=3,
            portnum_name="TELEMETRY_APP",
            timestamp=time.time(),  # newer → processed first (ORDER BY DESC)
        ),
    )
    store.insert_packet(
        settings.db_file,
        make_packet(
            from_node_id=X,
            gateway_node_id=GATEWAY,
            snr=10.0,
            rssi=-70,
            hop_limit=3,
            hop_start=3,
            portnum_name="TELEMETRY_APP",
            timestamp=time.time() - 60,
        ),
    )

    payload = graph.build_graph(settings, mode="rssi", use_cache=False)

    assert payload["stats"]["row_errors"] == 1
    assert payload["stats"]["receptions_analyzed"] == 2
    # The healthy row after the broken one still produced its link.
    assert {(l["source"], l["target"]) for l in payload["links"]} == {(GATEWAY, X)}


def test_truncated_flag_honours_the_limit(settings):
    for node in (A, X):
        store.insert_packet(
            settings.db_file,
            make_packet(
                from_node_id=node,
                gateway_node_id=GATEWAY,
                snr=7.0,
                rssi=-75,
                hop_limit=3,
                hop_start=3,
                portnum_name="TELEMETRY_APP",
            ),
        )

    capped = graph.build_graph(settings, mode="rssi", limit=1, use_cache=False)
    stats = capped["stats"]

    assert stats["truncated"] is True
    assert stats["rows_considered"] == 2  # saw one row past the limit
    assert stats["receptions_analyzed"] == 1  # showed only the limit

    full = graph.build_graph(settings, mode="rssi", limit=100, use_cache=False)
    assert full["stats"]["truncated"] is False
    assert full["stats"]["rows_considered"] == 2


def test_graph_response_carries_no_raw_payload(seeded):
    for mode in ("traceroute", "rssi", "combined"):
        payload = graph.build_graph(seeded, mode=mode, use_cache=False)
        assert "raw_payload" not in json.dumps(payload, ensure_ascii=False), mode


def test_cache_and_store_counters_are_reported(settings):
    first = graph.build_graph(settings, mode="traceroute", use_cache=True)

    assert "packets_deduplicated" in first["stats"]
    assert "packets_pruned_total" in first["stats"]
    assert first["stats"]["snr_scope"] == "both"
    misses, hits = first["stats"]["cache_misses"], first["stats"]["cache_hits"]

    second = graph.build_graph(settings, mode="traceroute", use_cache=True)
    assert second is first  # same key — nothing was recomputed
    assert second["stats"]["cache_misses"] == misses
    assert second["stats"]["cache_hits"] == hits + 1


def test_node_snr_counts_both_ends_of_every_hop(settings):
    # A→B→C→D: the old code let each node see only its outgoing hops.
    store.insert_packet(
        settings.db_file,
        make_packet(
            from_node_id=A,
            to_node_id=D,
            portnum_name="TRACEROUTE_APP",
            raw_payload=_route_payload(
                route=[B, C], snr_towards=[40, 44, 48]
            ),  # 10, 11, 12 dB
        ),
    )

    payload = graph.build_graph(settings, mode="traceroute", use_cache=False)
    by_id = {n["id"]: n for n in payload["nodes"]}

    assert by_id[A]["avg_snr"] == 10.0  # only A→B touches A from the front
    assert by_id[B]["avg_snr"] == 10.5  # (10 + 11) / 2 — both hops with B
    assert by_id[C]["avg_snr"] == 11.5  # (11 + 12) / 2
    assert by_id[D]["avg_snr"] == 12.0  # used to be None


def test_node_snr_does_not_jump_when_the_mode_changes(settings):
    # The same A pair seen once as a traceroute hop and once as a reception.
    store.insert_packet(
        settings.db_file,
        make_packet(
            from_node_id=A,
            to_node_id=B,
            portnum_name="TRACEROUTE_APP",
            raw_payload=_route_payload(route=[], snr_towards=[30]),  # 7.5 dB
        ),
    )
    store.insert_packet(
        settings.db_file,
        make_packet(
            from_node_id=A,
            gateway_node_id=GATEWAY,
            snr=7.5,
            rssi=-80,
            hop_limit=3,
            hop_start=3,
            portnum_name="TELEMETRY_APP",
        ),
    )

    traceroute = graph.build_graph(settings, mode="traceroute", use_cache=False)
    rssi = graph.build_graph(settings, mode="rssi", use_cache=False)
    t_node = {n["id"]: n for n in traceroute["nodes"]}[A]
    r_node = {n["id"]: n for n in rssi["nodes"]}[A]

    assert t_node["avg_snr"] == r_node["avg_snr"] == 7.5
