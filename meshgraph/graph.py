"""Build the graph payload the front end renders.

Two modes:

``traceroute``
    Faithful port of Malla's ``TracerouteService.get_network_graph_data``:
    RF hops parsed out of ``TRACEROUTE_APP`` payloads, plus the optional
    indirect (end-to-end) connections.

``rssi``
    Direct receptions: every packet a gateway heard with 0 remaining hops
    (``hop_start == hop_limit``) yields a gateway↔transmitter edge carrying the
    measured RSSI/SNR.  This covers *every* transmitting node, not only the
    ones that run traceroutes, which is why it produces a much denser graph.

``combined``
    Both of the above in one picture: the two sub-graphs are built with the
    same filters and merged (union of nodes and links, per-mode stats kept
    under ``stats.traceroute`` / ``stats.rssi``).
"""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Any

from . import store
from .config import GRAPH_MODES, Settings
from .traceroute import (
    BROADCAST_NODE_ID,
    SNR_INJECTED,
    SNR_UNKNOWN,
    build_rf_hops,
    is_plausible_rssi,
    is_plausible_snr,
    is_plausible_traceroute_snr,
)

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 5000
_CACHE_TTL_SECONDS = 20.0
_CACHE_MAX_ENTRIES = 64
_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()
# Single-flight: keys currently being rebuilt -> the event waiters block on
# until the leader publishes its result (or fails).
_inflight: dict[str, threading.Event] = {}

# Cumulative cache telemetry, mirrored into every /api/graph stats block.
_cache_stats = {"hits": 0, "misses": 0}


def _cache_fresh_locked(key: str) -> dict | None:
    """Return a non-expired payload; the caller already holds ``_cache_lock``.

    A hit is re-inserted at the end of the dict, so insertion order doubles
    as LRU order and eviction can drop the coldest key instead of clearing
    the whole cache.
    """
    entry = _cache.get(key)
    if entry is None:
        return None
    if time.time() - entry[0] >= _CACHE_TTL_SECONDS:
        _cache.pop(key, None)
        return None
    _cache.pop(key, None)
    _cache[key] = entry
    return entry[1]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _strength(avg_snr: float | None, avg_rssi: float | None, packet_count: int) -> float:
    """Visual edge weight in the 1..10 range, matching Malla's formula."""
    if avg_snr is not None:
        raw = (avg_snr + 20) / 5
    elif avg_rssi is not None:
        raw = (avg_rssi + 120) / 8
    else:
        raw = 1.0
    raw += math.log10(max(packet_count, 1))
    return round(min(10, max(1, raw)), 1)


def _node_size(packet_count: int) -> float:
    return round(min(20, max(5, math.log10(packet_count + 1) * 3)), 1)


def _count_row_error(stats: dict[str, Any], row_id: Any, exc: Exception) -> None:
    """One malformed row must never abort a whole build; count it for the UI."""
    stats["row_errors"] = stats.get("row_errors", 0) + 1
    logger.warning("Error processing packet %s: %s", row_id, exc)


def display_name(info: dict[str, Any] | None, node_id: int) -> str:
    if info:
        if info.get("long_name"):
            return str(info["long_name"])
        if info.get("short_name"):
            return str(info["short_name"])
        if info.get("hex_id"):
            return str(info["hex_id"])
    return f"!{node_id & 0xFFFFFFFF:08x}"


def _location(info: dict[str, Any] | None) -> dict[str, float] | None:
    if not info:
        return None
    if info.get("latitude") is None or info.get("longitude") is None:
        return None
    return {
        "latitude": info["latitude"],
        "longitude": info["longitude"],
        "altitude": info.get("altitude"),
    }


def _time_window(hours: int) -> tuple[float, float]:
    end = time.time()
    return end - hours * 3600, end


def _sanitize_snr(value: Any) -> float:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return -200.0
    # -200 is the "no limit" sentinel, anything outside the radio range is junk.
    if parsed < -200 or parsed > 20:
        return -200.0
    return parsed


def sanitize_hours(hours: int | None) -> int:
    try:
        value = int(hours)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 24
    return value if 1 <= value <= 720 else 24


# ---------------------------------------------------------------------------
# Mode: traceroute
# ---------------------------------------------------------------------------

def _build_traceroute(
    settings: Settings,
    hours: int,
    min_snr: float,
    include_indirect: bool,
    channel: str | None,
    limit: int,
) -> dict[str, Any]:
    start_time, _ = _time_window(hours)

    sql = """
        SELECT id, timestamp, from_node_id, to_node_id, gateway_id,
               hop_start, hop_limit, raw_payload
        FROM packets
        WHERE portnum_name = 'TRACEROUTE_APP'
          AND processed = 1
          AND timestamp >= ?
    """
    params: list[Any] = [start_time]
    if channel:
        sql += " AND channel_id = ?"
        params.append(channel)
    sql += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit + 1)

    packets = store.query(settings.db_file, sql, params)
    # Fetch one row past the limit: its presence proves the window does not
    # fit, so the sidebar can say so instead of pretending (G-P2-1).
    rows_considered = len(packets)
    truncated = rows_considered > limit
    if truncated:
        packets = packets[:limit]

    nodes: dict[int, dict[str, Any]] = {}
    direct_links: dict[tuple[int, int], dict[str, Any]] = {}
    indirect_connections: dict[tuple[int, int], dict[str, Any]] = {}

    stats: dict[str, Any] = {
        "mode": "traceroute",
        "packets_analyzed": len(packets),
        "rows_considered": rows_considered,
        "truncated": truncated,
        "packets_with_rf_hops": 0,
        "total_rf_hops": 0,
        "links_found": 0,
        "links_filtered_by_snr": 0,
        "links_filtered_due_to_snr_0": 0,
        "row_errors": 0,
    }

    def touch(node_id: int, timestamp: float) -> None:
        if node_id not in nodes:
            nodes[node_id] = {
                "id": node_id,
                "packet_count": 0,
                "total_snr": 0.0,
                "snr_count": 0,
                "connections": set(),
                "last_seen": timestamp,
            }
        node = nodes[node_id]
        node["packet_count"] += 1
        node["last_seen"] = max(node["last_seen"], timestamp)

    for row in packets:
        if not row["raw_payload"]:
            continue
        try:
            rf_hops = build_rf_hops(row)
            if not rf_hops:
                continue

            stats["packets_with_rf_hops"] += 1
            stats["total_rf_hops"] += len(rf_hops)

            for hop_from, hop_to, snr in rf_hops:
                if snr == SNR_INJECTED:
                    # Exactly 0 dB is an MQTT/UDP injected link, not an RF
                    # hop: a fake measurement, unlike a missing one (G-P1-3).
                    # Checked before min_snr so the reason never blends in.
                    stats["links_filtered_due_to_snr_0"] += 1
                    continue
                if not is_plausible_traceroute_snr(snr) or (
                    min_snr != -200 and snr < min_snr
                ):
                    stats["links_filtered_by_snr"] += 1
                    continue
                if BROADCAST_NODE_ID in (hop_from, hop_to):
                    continue

                touch(hop_from, row["timestamp"])
                touch(hop_to, row["timestamp"])

                link_key = tuple(sorted((hop_from, hop_to)))
                link = direct_links.get(link_key)
                if link is None:
                    direct_links[link_key] = {
                        "source": link_key[0],
                        "target": link_key[1],
                        "snr_values": [snr],
                        "packet_count": 1,
                        "last_seen": row["timestamp"],
                        "last_packet_id": row["id"],
                    }
                    stats["links_found"] += 1
                else:
                    link["snr_values"].append(snr)
                    link["packet_count"] += 1
                    if row["timestamp"] > link["last_seen"]:
                        link["last_seen"] = row["timestamp"]
                        link["last_packet_id"] = row["id"]

                nodes[hop_from]["connections"].add(hop_to)
                nodes[hop_to]["connections"].add(hop_from)
                # Both ends live through the same hop: counting it for both
                # keeps a node's avg_snr comparable across modes (G-P2-3,
                # stats.snr_scope = "both").
                nodes[hop_from]["total_snr"] += snr
                nodes[hop_from]["snr_count"] += 1
                nodes[hop_to]["total_snr"] += snr
                nodes[hop_to]["snr_count"] += 1

            if include_indirect and len(rf_hops) > 1:
                first_hop, last_hop = rf_hops[0], rf_hops[-1]
                indirect_key = tuple(sorted((first_hop[0], last_hop[1])))
                # Endpoints already connected by a direct hop need no indirect
                # line; a round trip that ends where it started would draw the
                # node as its own neighbour, which is not a connection.
                if (
                    indirect_key[0] != indirect_key[1]
                    and indirect_key not in direct_links
                ):
                    conn = indirect_connections.get(indirect_key)
                    if conn is None:
                        path_snrs = [
                            snr
                            for _, _, snr in rf_hops
                            if is_plausible_traceroute_snr(snr)
                        ]
                        indirect_connections[indirect_key] = {
                            "source": indirect_key[0],
                            "target": indirect_key[1],
                            "hop_count": len(rf_hops),
                            "path_count": 1,
                            "avg_snr": round(sum(path_snrs) / len(path_snrs), 1)
                            if path_snrs
                            else None,
                            "last_seen": row["timestamp"],
                            "last_packet_id": row["id"],
                        }
                    else:
                        conn["path_count"] += 1
                        conn["last_seen"] = max(conn["last_seen"], row["timestamp"])

        except Exception as exc:  # noqa: BLE001 - one bad packet must not abort
            _count_row_error(stats, row.get("id"), exc)
            continue

    processed_links = []
    for link in direct_links.values():
        avg_snr = sum(link["snr_values"]) / len(link["snr_values"])
        processed_links.append(
            {
                "source": link["source"],
                "target": link["target"],
                "type": "direct",
                "avg_snr": round(avg_snr, 1),
                "packet_count": link["packet_count"],
                "strength": _strength(avg_snr, None, link["packet_count"]),
                "last_seen": link["last_seen"],
            }
        )

    processed_indirect = []
    if include_indirect:
        for conn in indirect_connections.values():
            processed_indirect.append(
                {
                    "source": conn["source"],
                    "target": conn["target"],
                    "type": "indirect",
                    "hop_count": conn["hop_count"],
                    "path_count": conn["path_count"],
                    "avg_snr": conn["avg_snr"],
                    "strength": min(
                        5, max(0.5, conn["path_count"] / conn["hop_count"])
                    ),
                    "last_seen": conn["last_seen"],
                }
            )

    return _finish(
        settings=settings,
        mode="traceroute",
        hours=hours,
        min_snr=min_snr,
        include_indirect=include_indirect,
        channel=channel,
        nodes=nodes,
        links=processed_links,
        indirect=processed_indirect,
        stats=stats,
        start_time=start_time,
        node_snr_from="total_snr",
    )


# ---------------------------------------------------------------------------
# Mode: RSSI (direct receptions)
# ---------------------------------------------------------------------------

def _build_rssi(
    settings: Settings,
    hours: int,
    min_snr: float,
    include_indirect: bool,  # not meaningful here; kept for API symmetry
    channel: str | None,
    limit: int,
) -> dict[str, Any]:
    start_time, _ = _time_window(hours)

    common_where = """
        WHERE timestamp >= ?
          AND gateway_node_id IS NOT NULL
          AND from_node_id IS NOT NULL
          AND from_node_id != gateway_node_id
    """
    common_params: list[Any] = [start_time]
    if channel:
        common_where += " AND channel_id = ?"
        common_params.append(channel)

    # Only packets whose hop budget is untouched were heard directly by the
    # gateway; anything with hop_limit < hop_start came in via a relay and
    # says nothing about the RF link between gateway and transmitter.
    sql = (
        "SELECT id, timestamp, from_node_id, gateway_node_id, gateway_id, rssi, snr "
        "FROM packets "
        + common_where
        + " AND hop_start IS NOT NULL AND hop_limit IS NOT NULL AND hop_start = hop_limit"
        + " ORDER BY timestamp DESC LIMIT ?"
    )
    rows = store.query(settings.db_file, sql, [*common_params, limit + 1])
    rows_considered = len(rows)
    truncated = rows_considered > limit
    if truncated:
        rows = rows[:limit]

    relayed_sql = (
        "SELECT COUNT(*) AS n FROM packets "
        + common_where
        + " AND (hop_start IS NULL OR hop_limit IS NULL OR hop_start != hop_limit)"
    )
    relayed = store.query(settings.db_file, relayed_sql, common_params)
    relayed_count = int(relayed[0]["n"]) if relayed else 0

    nodes: dict[int, dict[str, Any]] = {}
    links_raw: dict[tuple[int, int], dict[str, Any]] = {}

    stats: dict[str, Any] = {
        "mode": "rssi",
        "receptions_analyzed": len(rows),
        "rows_considered": rows_considered,
        "truncated": truncated,
        "receptions_relayed": relayed_count,
        "receptions_plausible": 0,
        "receptions_snr_injected": 0,
        "receptions_snr_unknown": 0,
        "links_found": 0,
        "links_filtered": 0,
        # Reasons stay separate so the sidebar never lumps them together.
        "links_filtered_no_signal": 0,
        "links_filtered_below_min_snr": 0,
        "row_errors": 0,
    }

    def touch(node_id: int, timestamp: float, snr: float | None, rssi: float | None):
        if node_id not in nodes:
            nodes[node_id] = {
                "id": node_id,
                "packet_count": 0,
                "snr_values": [],
                "rssi_values": [],
                "connections": set(),
                "last_seen": timestamp,
            }
        node = nodes[node_id]
        node["packet_count"] += 1
        node["last_seen"] = max(node["last_seen"], timestamp)
        if snr is not None:
            node["snr_values"].append(snr)
        if rssi is not None:
            node["rssi_values"].append(rssi)

    for row in rows:
        try:
            raw_snr = row["snr"]
            if raw_snr == SNR_INJECTED:
                # Exactly 0 dB is an MQTT/UDP injected reception: a fake
                # measurement (G-P1-3), kept out of every average.
                snr = SNR_UNKNOWN
                stats["receptions_snr_injected"] += 1
            elif is_plausible_snr(raw_snr):
                snr = float(raw_snr)
            else:
                # NULL or out of range: there is no usable measurement.
                snr = SNR_UNKNOWN
                stats["receptions_snr_unknown"] += 1
            rssi = float(row["rssi"]) if is_plausible_rssi(row["rssi"]) else None

            if snr is None and rssi is None:
                # No measurement at all — its own reason, not a filter hit.
                stats["links_filtered"] += 1
                stats["links_filtered_no_signal"] += 1
                continue
            if min_snr != -200 and (snr is None or snr < min_snr):
                stats["links_filtered"] += 1
                stats["links_filtered_below_min_snr"] += 1
                continue

            stats["receptions_plausible"] += 1

            gateway_id = int(row["gateway_node_id"])
            from_id = int(row["from_node_id"])
            link_key = tuple(sorted((gateway_id, from_id)))

            touch(gateway_id, row["timestamp"], snr, rssi)
            touch(from_id, row["timestamp"], snr, rssi)
            nodes[gateway_id]["connections"].add(from_id)
            nodes[from_id]["connections"].add(gateway_id)

            link = links_raw.get(link_key)
            if link is None:
                links_raw[link_key] = {
                    "source": link_key[0],
                    "target": link_key[1],
                    "snr_values": [snr] if snr is not None else [],
                    "rssi_values": [rssi] if rssi is not None else [],
                    "packet_count": 1,
                    "last_seen": row["timestamp"],
                }
                stats["links_found"] += 1
            else:
                link["packet_count"] += 1
                if snr is not None:
                    link["snr_values"].append(snr)
                if rssi is not None:
                    link["rssi_values"].append(rssi)
                if row["timestamp"] > link["last_seen"]:
                    link["last_seen"] = row["timestamp"]
        except Exception as exc:  # noqa: BLE001 - one bad row must not abort
            _count_row_error(stats, row.get("id"), exc)
            continue

    processed_links = []
    for link in links_raw.values():
        avg_snr = (
            sum(link["snr_values"]) / len(link["snr_values"])
            if link["snr_values"]
            else None
        )
        avg_rssi = (
            sum(link["rssi_values"]) / len(link["rssi_values"])
            if link["rssi_values"]
            else None
        )
        processed_links.append(
            {
                "source": link["source"],
                "target": link["target"],
                "type": "direct",
                "avg_snr": round(avg_snr, 1) if avg_snr is not None else None,
                "avg_rssi": round(avg_rssi, 1) if avg_rssi is not None else None,
                "packet_count": link["packet_count"],
                "strength": _strength(avg_snr, avg_rssi, link["packet_count"]),
                "last_seen": link["last_seen"],
            }
        )

    return _finish(
        settings=settings,
        mode="rssi",
        hours=hours,
        min_snr=min_snr,
        include_indirect=False,
        channel=channel,
        nodes=nodes,
        links=processed_links,
        indirect=[],
        stats=stats,
        start_time=start_time,
        node_snr_from="snr_values",
    )


# ---------------------------------------------------------------------------
# Mode: combined (traceroute + direct receptions)
# ---------------------------------------------------------------------------

# Fields where an empty value in the traceroute view can be filled from the
# reception view (and vice versa): names/roles/coordinates come from the same
# node lookup in both, SNR/RSSI describe different measurements.
_COMBINED_NODE_KEYS = ("avg_snr", "avg_rssi", "name", "hex_id", "role", "hw_model", "location")


def _build_combined(
    settings: Settings,
    hours: int,
    min_snr: float,
    include_indirect: bool,
    channel: str | None,
    limit: int,
) -> dict[str, Any]:
    """Traceroute hops and direct receptions drawn as a single graph.

    Both sub-graphs are built with the caller's filters and then unioned.
    A link seen by both modes keeps one row with ``modes`` listing where it
    came from: SNR prefers the traceroute measurement, RSSI comes from the
    receptions.  Node counters take the per-mode maximum rather than the sum,
    because the very same packet feeds both sub-graphs.
    """
    trace = _build_traceroute(
        settings, hours, min_snr, include_indirect, channel, limit
    )
    rssi = _build_rssi(settings, hours, min_snr, include_indirect, channel, limit)

    nodes: dict[int, dict[str, Any]] = {}
    for payload in (trace, rssi):
        for node in payload["nodes"]:
            current = nodes.get(node["id"])
            if current is None:
                nodes[node["id"]] = dict(node)
                continue
            current["packet_count"] = max(
                current["packet_count"], node["packet_count"]
            )
            current["last_seen"] = max(current["last_seen"], node["last_seen"])
            current["size"] = max(current["size"], node["size"])
            current["is_gateway"] = current["is_gateway"] or node["is_gateway"]
            for key in _COMBINED_NODE_KEYS:
                if current.get(key) in (None, "") and node.get(key) not in (None, ""):
                    current[key] = node[key]

    links: dict[tuple[int, int], dict[str, Any]] = {}
    for payload, kind in ((trace, "traceroute"), (rssi, "rssi")):
        for link in payload["links"]:
            key = (link["source"], link["target"])
            current = links.get(key)
            if current is None:
                merged = dict(link)
                merged["modes"] = [kind]
                links[key] = merged
                continue
            current["modes"].append(kind)
            current["packet_count"] = max(
                current["packet_count"], link["packet_count"]
            )
            current["last_seen"] = max(current["last_seen"], link["last_seen"])
            if current.get("avg_snr") is None:
                current["avg_snr"] = link.get("avg_snr")
            if current.get("avg_rssi") is None:
                current["avg_rssi"] = link.get("avg_rssi")
            current["strength"] = _strength(
                current.get("avg_snr"),
                current.get("avg_rssi"),
                current["packet_count"],
            )

    # Each sub-graph counted neighbours inside itself only; recompute the
    # degree from the merged edge set so the sidebar reports the union.
    neighbors: dict[int, set[int]] = {}
    for source, target in links:
        neighbors.setdefault(source, set()).add(target)
        neighbors.setdefault(target, set()).add(source)
    for node_id, node in nodes.items():
        node["connections"] = len(neighbors.get(node_id, set()))

    indirect = trace["indirect_connections"]
    trace_stats = trace["stats"]
    rssi_stats = rssi["stats"]
    stats: dict[str, Any] = {
        "mode": "combined",
        # One number per sidebar row; the full per-mode detail is nested.
        "packets_analyzed": rssi_stats.get("receptions_analyzed", 0),
        "receptions_analyzed": rssi_stats.get("receptions_analyzed", 0),
        "receptions_relayed": rssi_stats.get("receptions_relayed", 0),
        "packets_with_rf_hops": trace_stats.get("packets_with_rf_hops", 0),
        "total_rf_hops": trace_stats.get("total_rf_hops", 0),
        "links_filtered": rssi_stats.get("links_filtered", 0),
        "links_filtered_by_snr": trace_stats.get("links_filtered_by_snr", 0),
        "row_errors": trace_stats.get("row_errors", 0) + rssi_stats.get("row_errors", 0),
        "rows_considered": trace_stats.get("rows_considered", 0)
        + rssi_stats.get("rows_considered", 0),
        "truncated": bool(trace_stats.get("truncated") or rssi_stats.get("truncated")),
        "traceroute": trace_stats,
        "rssi": rssi_stats,
        "nodes": len(nodes),
        "links": len(links),
        "indirect": len(indirect),
        "gateways": sum(1 for node in nodes.values() if node["is_gateway"]),
    }

    return {
        "mode": "combined",
        "nodes": list(nodes.values()),
        "links": sorted(links.values(), key=lambda l: -l["packet_count"]),
        "indirect_connections": indirect,
        "stats": stats,
        "filters": trace["filters"],
        "generated_at": time.time(),
    }


# ---------------------------------------------------------------------------
# Shared finishing step
# ---------------------------------------------------------------------------

def _finish(
    *,
    settings: Settings,
    mode: str,
    hours: int,
    min_snr: float,
    include_indirect: bool,
    channel: str | None,
    nodes: dict[int, dict[str, Any]],
    links: list[dict[str, Any]],
    indirect: list[dict[str, Any]],
    stats: dict[str, Any],
    start_time: float,
    node_snr_from: str,
) -> dict[str, Any]:
    node_ids = list(nodes)
    lookup = store.node_lookup(settings.db_file, node_ids)
    gateways = store.gateway_ids(settings.db_file, start_time)

    processed_nodes = []
    for node_id, data in nodes.items():
        info = lookup.get(node_id)

        snr_source = data.get(node_snr_from)
        avg_snr = None
        if isinstance(snr_source, list):
            if snr_source:
                avg_snr = round(sum(snr_source) / len(snr_source), 1)
        elif data.get("snr_count"):
            avg_snr = round(data["total_snr"] / data["snr_count"], 1)

        rssi_source = data.get("rssi_values")
        avg_rssi = (
            round(sum(rssi_source) / len(rssi_source), 1) if rssi_source else None
        )

        node_info = {
            "id": node_id,
            "name": display_name(info, node_id),
            "hex_id": (info or {}).get("hex_id") or f"!{node_id & 0xFFFFFFFF:08x}",
            "packet_count": data["packet_count"],
            "connections": len(data["connections"]),
            "avg_snr": avg_snr,
            "avg_rssi": avg_rssi,
            "last_seen": data["last_seen"],
            "size": _node_size(data["packet_count"]),
            "is_gateway": node_id in gateways,
            "role": (info or {}).get("role"),
            "hw_model": (info or {}).get("hw_model"),
        }
        loc = _location(info)
        if loc:
            node_info["location"] = loc
        processed_nodes.append(node_info)

    processed_links = sorted(links, key=lambda l: -l["packet_count"])

    stats.update(
        {
            "nodes": len(processed_nodes),
            "links": len(processed_links),
            "indirect": len(indirect),
            "gateways": sum(1 for n in processed_nodes if n["is_gateway"]),
        }
    )

    return {
        "mode": mode,
        "nodes": processed_nodes,
        "links": processed_links,
        "indirect_connections": indirect,
        "stats": stats,
        "filters": {
            "hours": hours,
            "min_snr": min_snr,
            "include_indirect": include_indirect,
            "channel": channel or "",
        },
        "generated_at": time.time(),
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def build_graph(
    settings: Settings,
    mode: str = "traceroute",
    hours: int = 24,
    min_snr: float = -200.0,
    include_indirect: bool = False,
    channel: str | None = None,
    limit: int | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Build the graph JSON for the requested filters."""
    hours = sanitize_hours(hours)
    min_snr = _sanitize_snr(min_snr)
    channel = (channel or "").strip() or None
    limit = limit or settings.graph_packet_limit or DEFAULT_LIMIT

    if mode not in GRAPH_MODES:
        mode = "traceroute"

    cache_key = "|".join(
        [
            settings.db_file,
            mode,
            str(hours),
            str(min_snr),
            str(bool(include_indirect)),
            channel or "-",
            str(limit),
            # Changed data (new packet, prune) means a new key: fresh rows
            # become visible immediately, without waiting for the TTL.
            str(store.generation(settings.db_file)),
        ]
    )

    def compute() -> dict[str, Any]:
        if mode == "rssi":
            return _build_rssi(
                settings, hours, min_snr, include_indirect, channel, limit
            )
        if mode == "combined":
            return _build_combined(
                settings, hours, min_snr, include_indirect, channel, limit
            )
        return _build_traceroute(
            settings, hours, min_snr, include_indirect, channel, limit
        )

    def finish(payload: dict[str, Any]) -> dict[str, Any]:
        # Request-path and process-wide telemetry added to every response, so
        # the sidebar gets it without mode-specific plumbing (G-P2-1).
        payload["stats"]["cache_hits"] = _cache_stats["hits"]
        payload["stats"]["cache_misses"] = _cache_stats["misses"]
        payload["stats"]["snr_scope"] = "both"
        payload["stats"].update(store.counters(settings.db_file))
        return payload

    if not use_cache:
        return finish(compute())

    # Single-flight: the first thread on a key becomes the leader and
    # computes; everyone else waits for its event and re-checks the cache
    # (a leader that failed raises and clears the way for a new one).
    while True:
        with _cache_lock:
            cached = _cache_fresh_locked(cache_key)
            if cached is None:
                event = _inflight.get(cache_key)
                if event is None:
                    _inflight[cache_key] = event = threading.Event()
                    _cache_stats["misses"] += 1
                    break
        if cached is not None:
            _cache_stats["hits"] += 1
            return finish(cached)
        event.wait()

    try:
        payload = compute()
    except BaseException:
        with _cache_lock:
            _inflight.pop(cache_key, None)
        event.set()
        raise

    with _cache_lock:
        _cache[cache_key] = (time.time(), payload)
        # Evict the least recently used key instead of clearing everything:
        # a stream of distinct filters must not drop the hot one.
        while len(_cache) > _CACHE_MAX_ENTRIES:
            _cache.pop(next(iter(_cache)))
        _inflight.pop(cache_key, None)
    event.set()
    return finish(payload)


# ---------------------------------------------------------------------------
# Packet routes (replay data for the glowing-packet animation)
# ---------------------------------------------------------------------------
#
# Two stories the front end replays on the graph (packetflow.js):
#
# ``fan``         one mesh packet heard by two or more gateways — legs fan
#                 out from the sender to each gateway.  Only direct receptions
#                 count (hop_start == hop_limit), so every leg is an edge the
#                 rssi half of the graph draws.
# ``traceroute``  a walk along the RF hops parsed from a TRACEROUTE_APP
#                 payload — the packet crawls node by node.
#
# Pure data: nothing is filtered by the current graph mode here, the front
# end drops legs whose nodes or edges are not on screen.

DEFAULT_ROUTE_LIMIT = 40
MAX_ROUTE_LIMIT = 100
REPLAY_MINUTES_DEFAULT = 30
REPLAY_MINUTES_MIN = 1
REPLAY_MINUTES_MAX = 7 * 24 * 60


def sanitize_minutes(minutes: Any) -> int:
    try:
        value = int(minutes)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return REPLAY_MINUTES_DEFAULT
    if REPLAY_MINUTES_MIN <= value <= REPLAY_MINUTES_MAX:
        return value
    return REPLAY_MINUTES_DEFAULT


def _fan_routes(
    settings: Settings, start_time: float, limit: int
) -> list[dict[str, Any]]:
    """Packets heard by >= 2 gateways; one leg per gateway, freshest first."""
    groups = store.query(
        settings.db_file,
        """
        SELECT from_node_id, mesh_packet_id, MIN(timestamp) AS first_ts
        FROM packets
        WHERE timestamp >= ?
          AND mesh_packet_id IS NOT NULL
          AND from_node_id IS NOT NULL
          AND gateway_node_id IS NOT NULL
          AND gateway_node_id != from_node_id
          AND hop_start IS NOT NULL AND hop_start = hop_limit
          AND portnum_name IS NOT 'TRACEROUTE_APP'
        GROUP BY from_node_id, mesh_packet_id
        HAVING COUNT(DISTINCT gateway_node_id) >= 2
        ORDER BY first_ts DESC
        LIMIT ?
        """,
        (start_time, limit),
    )
    if not groups:
        return []

    keys = [(g["from_node_id"], g["mesh_packet_id"]) for g in groups]
    placeholders = ",".join("(?, ?)" for _ in keys)
    params: list[Any] = [start_time]
    for sender, packet_id in keys:
        params.extend((sender, packet_id))
    rows = store.query(
        settings.db_file,
        f"""
        SELECT from_node_id, mesh_packet_id, gateway_node_id, timestamp, rssi, snr
        FROM packets
        WHERE timestamp >= ?
          AND (from_node_id, mesh_packet_id) IN ({placeholders})
          AND gateway_node_id IS NOT NULL
          AND gateway_node_id != from_node_id
          AND hop_start IS NOT NULL AND hop_start = hop_limit
        ORDER BY timestamp
        """,
        params,
    )

    # One leg per gateway: dedup normally keeps a single row, but if a
    # duplicate slipped through, the strongest reception wins.
    best: dict[tuple[int, int, int], dict[str, Any]] = {}
    for row in rows:
        key = (row["from_node_id"], row["mesh_packet_id"], row["gateway_node_id"])
        prev = best.get(key)
        if prev is None or (row["rssi"] or -999) > (prev["rssi"] or -999):
            best[key] = row
    by_packet: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for (sender, packet_id, _gateway), row in best.items():
        by_packet.setdefault((sender, packet_id), []).append(row)

    routes: list[dict[str, Any]] = []
    for sender, packet_id in keys:
        legs = by_packet.get((sender, packet_id))
        if not legs or len(legs) < 2:
            continue
        legs.sort(key=lambda row: row["timestamp"])
        routes.append(
            {
                "kind": "fan",
                "sender": sender,
                "packet_id": packet_id,
                "ts": legs[0]["timestamp"],
                "legs": [
                    {
                        "to": row["gateway_node_id"],
                        "ts": row["timestamp"],
                        "rssi": row["rssi"],
                        "snr": row["snr"],
                    }
                    for row in legs
                ],
            }
        )
    return routes


def _traceroute_routes(
    settings: Settings, start_time: float, limit: int
) -> list[dict[str, Any]]:
    """RF hop walks from TRACEROUTE_APP payloads, freshest first."""
    rows = store.query(
        settings.db_file,
        """
        SELECT id, timestamp, from_node_id, to_node_id, hop_start, hop_limit,
               mesh_packet_id, raw_payload
        FROM packets
        WHERE portnum_name = 'TRACEROUTE_APP'
          AND processed = 1
          AND timestamp >= ?
        ORDER BY timestamp DESC
        LIMIT ?
        """,
        (start_time, limit),
    )
    routes: list[dict[str, Any]] = []
    for row in rows:
        if not row["raw_payload"]:
            continue
        try:
            hops = build_rf_hops(row)
        except Exception:  # malformed payload: skip the row, keep the routes
            logger.debug("Route payload %s failed to parse", row["id"], exc_info=True)
            continue
        if not hops:
            continue
        if any(BROADCAST_NODE_ID in (a, b) for a, b, _snr in hops):
            # A chain through a placeholder id cannot be drawn on the graph.
            continue
        routes.append(
            {
                "kind": "traceroute",
                "sender": hops[0][0],
                "packet_id": row["mesh_packet_id"],
                "ts": row["timestamp"],
                "legs": [
                    {
                        "to": hop_to,
                        "ts": row["timestamp"],
                        # Unknown or injected (MQTT) SNR keeps the leg — the
                        # chain must not break — but loses its colour.
                        "snr": (
                            snr
                            if is_plausible_traceroute_snr(snr)
                            and snr != SNR_INJECTED
                            else None
                        ),
                    }
                    for _hop_from, hop_to, snr in hops
                ],
            }
        )
    return routes


def packet_routes(
    settings: Settings,
    minutes: int | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Replayable routes: who heard the same packet, and along which hops.

    Both sources share one window and one cap; the freshest ``limit`` routes
    come back in chronological order so the animation follows real time.
    """
    minutes = sanitize_minutes(minutes)
    try:
        parsed_limit = int(limit)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        parsed_limit = DEFAULT_ROUTE_LIMIT
    parsed_limit = max(1, min(parsed_limit, MAX_ROUTE_LIMIT))
    start_time = time.time() - minutes * 60

    routes = _fan_routes(settings, start_time, parsed_limit)
    routes += _traceroute_routes(settings, start_time, parsed_limit)
    routes.sort(key=lambda route: route["ts"])
    routes = routes[-parsed_limit:]
    return {
        "minutes": minutes,
        "generated_at": time.time(),
        "routes": routes,
    }


def invalidate_cache() -> None:
    with _cache_lock:
        _cache.clear()
