"""RouteDiscovery parsing and RF-hop extraction."""

from __future__ import annotations

from meshtastic import mesh_pb2

from meshgraph.traceroute import (
    TRACEROUTE_UNKNOWN_SNR,
    build_rf_hops,
    is_plausible_rssi,
    is_plausible_snr,
    is_plausible_traceroute_snr,
    parse_traceroute_payload,
    rf_hops,
)

A, B, C, D = 0x1111, 0x2222, 0x3333, 0x4444


def _payload(route=(), snr_towards=(), route_back=(), snr_back=()) -> bytes:
    msg = mesh_pb2.RouteDiscovery()
    msg.route.extend(route)
    msg.snr_towards.extend(snr_towards)
    msg.route_back.extend(route_back)
    msg.snr_back.extend(snr_back)
    return msg.SerializeToString()


# ---------------------------------------------------------------------------
# Plausibility
# ---------------------------------------------------------------------------

def test_rssi_plausibility_bounds():
    assert is_plausible_rssi(-95)
    assert not is_plausible_rssi(0)      # "not provided" sentinel
    assert not is_plausible_rssi(-1386841926)  # corrupt gateway frame
    assert not is_plausible_rssi(None)


def test_snr_plausibility_bounds():
    assert is_plausible_snr(0.0)
    assert is_plausible_snr(-12.5)
    assert not is_plausible_snr(-45)   # below any LoRa link budget
    assert not is_plausible_snr(45)    # above the radio's ceiling
    assert not is_plausible_snr(None)


def test_traceroute_snr_keeps_unknown_sentinel():
    assert is_plausible_traceroute_snr(TRACEROUTE_UNKNOWN_SNR)
    assert is_plausible_traceroute_snr(-7.5)
    assert not is_plausible_traceroute_snr(-99.0)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def test_parse_scalers_snr_to_db():
    parsed = parse_traceroute_payload(
        _payload(route=[B, C], snr_towards=[40, 44], route_back=[B], snr_back=[48])
    )
    assert parsed["route_nodes"] == [B, C]
    assert parsed["snr_towards"] == [10.0, 11.0]
    assert parsed["route_back"] == [B]
    assert parsed["snr_back"] == [12.0]


def test_parse_garbage_returns_empty():
    assert parse_traceroute_payload(b"")["route_nodes"] == []
    # Valid protobuf framing but nonsense field types still fail cleanly.
    assert parse_traceroute_payload(b"\xff\xff\xff\xff")["route_nodes"] == []


# ---------------------------------------------------------------------------
# RF hops
# ---------------------------------------------------------------------------

def test_direct_hop_without_intermediate_nodes():
    hops = rf_hops(A, D, {"route_nodes": [], "snr_towards": [8.0]})
    assert hops == [(A, D, 8.0)]


def test_multi_hop_forward_path():
    # A completed 3-hop trip: the packet arrived with its hop budget exhausted.
    data = {"route_nodes": [B, C], "snr_towards": [4.0, 8.0, 12.0]}
    assert rf_hops(A, D, data, hop_start=3, hop_limit=0) == [
        (A, B, 4.0),
        (B, C, 8.0),
        (C, D, 12.0),
    ]


def test_hop_budget_decides_forward_vs_return_interpretation():
    """Malla's rule: route_nodes > (hop_start - hop_limit) means "going back"."""
    data = {"route_nodes": [B, C], "snr_towards": [4.0, 8.0, 12.0]}

    forward = rf_hops(A, D, data, hop_start=3, hop_limit=0)
    assert forward[0] == (A, B, 4.0)

    # Budget barely touched → the packet is on its way back to the originator,
    # so the forward leg is read destination-first.
    returning = rf_hops(A, D, data, hop_start=3, hop_limit=3)
    assert returning[0] == (D, B, 4.0)


def test_missing_hop_budget_assumes_forward_journey():
    """Absent counters must not be read as an exhausted budget (see _is_going_back)."""
    data = {"route_nodes": [B, C], "snr_towards": [4.0, 8.0, 12.0]}
    assert rf_hops(A, D, data)[0] == (A, B, 4.0)
    assert rf_hops(A, D, data, hop_start=None, hop_limit=0)[0] == (A, B, 4.0)


def test_hops_stopped_when_snr_values_run_out():
    data = {"route_nodes": [B, C], "snr_towards": [4.0]}
    assert rf_hops(A, D, data) == [(A, B, 4.0)]


def test_return_journey_contributes_hops():
    data = {
        "route_nodes": [B],
        "snr_towards": [4.0],
        "route_back": [B],
        "snr_back": [10.0],
    }
    hops = rf_hops(A, D, data, hop_start=2, hop_limit=1)
    # Forward leg is interpreted as destination-first because a return path exists.
    assert (D, B, 4.0) in hops
    # Return leg runs origin -> route_back -> destination when SNR has the extra hop.
    assert (A, B, 10.0) in hops


def test_short_return_payload_only_covers_known_hops():
    data = {
        "route_nodes": [B],
        "snr_towards": [4.0],
        "route_back": [B],
        "snr_back": [10.0, 6.0],
    }
    hops = rf_hops(A, D, data, hop_start=2, hop_limit=1)
    # snr_back has 2 values but only 1 route_back node → A -> B -> D.
    assert (A, B, 10.0) in hops
    assert (B, D, 6.0) in hops


def test_missing_endpoints_yield_nothing():
    assert rf_hops(None, D, {"route_nodes": [], "snr_towards": [4.0]}) == []


def test_build_rf_hops_reads_row_fields():
    # RouteDiscovery stores SNR as quarter-dB integers: 40 → 10.0 dB.
    row = {
        "from_node_id": A,
        "to_node_id": D,
        "hop_start": 3,
        "hop_limit": 0,
        "raw_payload": _payload(route=[B, C], snr_towards=[40, 44, 48]),
    }
    assert build_rf_hops(row) == [(A, B, 10.0), (B, C, 11.0), (C, D, 12.0)]


def test_build_rf_hops_handles_missing_payload():
    assert build_rf_hops({"from_node_id": A, "to_node_id": D}) == []
