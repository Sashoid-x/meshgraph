"""Signal plausibility bounds and traceroute payload parsing.

Ported from Malla's ``utils/signal_quality.py`` and
``utils/traceroute_utils.py`` / ``models/traceroute.py``.
"""

from __future__ import annotations

import logging
import math
from typing import Any

logger = logging.getLogger(__name__)

RSSI_PLAUSIBLE_MIN = -150  # dBm, below any LoRa sensitivity floor
RSSI_PLAUSIBLE_MAX = -1  # dBm, 0 is the "not provided" sentinel
SNR_PLAUSIBLE_MIN = -30.0
SNR_PLAUSIBLE_MAX = 30.0

# RouteDiscovery encodes "SNR unknown" as INT8_MIN, which scales to -32.0 dB.
TRACEROUTE_UNKNOWN_SNR = -32.0

# Meshtastic broadcast / invalid node id.
BROADCAST_NODE_ID = 0xFFFFFFFF


def is_plausible_rssi(value: float | int | None) -> bool:
    return (
        value is not None
        and math.isfinite(value)
        and RSSI_PLAUSIBLE_MIN <= value <= RSSI_PLAUSIBLE_MAX
    )


def is_plausible_snr(value: float | int | None) -> bool:
    return (
        value is not None
        and math.isfinite(value)
        and SNR_PLAUSIBLE_MIN <= value <= SNR_PLAUSIBLE_MAX
    )


def is_plausible_traceroute_snr(value: float | int | None) -> bool:
    """Like :func:`is_plausible_snr` but keeps the -32.0 "unknown" sentinel."""
    return value is not None and (
        value == TRACEROUTE_UNKNOWN_SNR or is_plausible_snr(value)
    )


def parse_traceroute_payload(raw_payload: bytes) -> dict[str, list]:
    """Parse a ``RouteDiscovery`` payload into route + SNR arrays.

    SNR values are quarter-dB integers in protobuf and are scaled to dB here.
    Returns empty lists for anything that does not parse.
    """
    empty: dict[str, list] = {
        "route_nodes": [],
        "snr_towards": [],
        "route_back": [],
        "snr_back": [],
    }
    if not raw_payload:
        return empty

    try:
        from meshtastic import mesh_pb2

        route_discovery = mesh_pb2.RouteDiscovery()
        route_discovery.ParseFromString(raw_payload)
        return {
            "route_nodes": [int(n) for n in route_discovery.route],
            "snr_towards": [float(s) / 4.0 for s in route_discovery.snr_towards],
            "route_back": [int(n) for n in route_discovery.route_back],
            "snr_back": [float(s) / 4.0 for s in route_discovery.snr_back],
        }
    except Exception as exc:  # noqa: BLE001 - malformed payload, just skip it
        logger.debug("Traceroute payload parse failed: %s", exc)
        return empty


def _has_return_path(route_back: list[int]) -> bool:
    return bool(route_back)


def _is_going_back(
    route: list[int],
    snr_towards: list[float],
    hop_start: int | None,
    hop_limit: int | None,
) -> bool:
    """Decide whether the packet is on its return journey.

    Malla compares ``len(route_nodes)`` against ``hop_start - hop_limit``.  When
    both counters are missing it computes ``0 - 0`` and therefore reads every
    completed traceroute as a return trip.  Since the hop budget is the only
    thing that disambiguates the two readings, absent data gets the ordinary
    forward interpretation instead.
    """
    if hop_start is None or hop_limit is None:
        return False
    return (len(snr_towards) > len(route)) and len(route) > (hop_start - hop_limit)


def rf_hops(
    from_node_id: int | None,
    to_node_id: int | None,
    route_data: dict[str, list],
    hop_start: int | None = None,
    hop_limit: int | None = None,
) -> list[tuple[int, int, float]]:
    """Extract the RF hops a traceroute packet actually took.

    Straight port of Malla's ``TraceroutePacket._determine_actual_rf_path``:
    forward hops are derived from ``snr_towards`` (mind the "return journey"
    cases where the packet is travelling back to its originator), then the
    return journey contributes its own hops from ``snr_back``.

    Returns ``(from_node_id, to_node_id, snr_db)`` triples.
    """
    if from_node_id is None or to_node_id is None:
        return []

    route = route_data.get("route_nodes", []) or []
    snr_towards = route_data.get("snr_towards", []) or []
    route_back = route_data.get("route_back", []) or []
    snr_back = route_data.get("snr_back", []) or []

    hops: list[tuple[int, int, float]] = []
    has_return = _has_return_path(route_back)

    # --- forward journey -------------------------------------------------
    if snr_towards:
        if has_return or _is_going_back(route, snr_towards, hop_start, hop_limit):
            forward_ids = [to_node_id] + route + [from_node_id]
        else:
            forward_ids = [from_node_id] + route + [to_node_id]

        if not route and snr_towards:
            # Direct hop with no intermediate nodes.
            forward_ids = [from_node_id, to_node_id] if not has_return else [
                to_node_id,
                from_node_id,
            ]

        for i in range(len(forward_ids) - 1):
            if i < len(snr_towards):
                hops.append((forward_ids[i], forward_ids[i + 1], snr_towards[i]))

    # --- return journey --------------------------------------------------
    if has_return and snr_back:
        actual = min(len(route_back), len(snr_back))
        if actual > 0:
            return_ids = [from_node_id] + route_back[:actual]
            if len(snr_back) > actual:
                return_ids.append(to_node_id)
        else:
            return_ids = [from_node_id]

        for i in range(len(return_ids) - 1):
            if i < len(snr_back):
                hops.append((return_ids[i], return_ids[i + 1], snr_back[i]))

    return hops


def build_rf_hops(
    packet: dict[str, Any]
) -> list[tuple[int, int, float]]:
    """Convenience wrapper taking a ``packets`` row."""
    route_data = parse_traceroute_payload(packet.get("raw_payload") or b"")
    return rf_hops(
        packet.get("from_node_id"),
        packet.get("to_node_id"),
        route_data,
        hop_start=packet.get("hop_start"),
        hop_limit=packet.get("hop_limit"),
    )
