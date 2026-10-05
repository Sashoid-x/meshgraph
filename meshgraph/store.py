"""SQLite storage for packets and node metadata.

Deliberately much smaller than Malla's schema: the graph needs raw traceroute
payloads, RSSI/SNR of each reception, and node names/coordinates.  Everything
else is noise here.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Sequence

from .config import Settings
from .decoder import DecodedPacket

logger = logging.getLogger(__name__)

_lock = threading.RLock()
_initialized_for: str | None = None

# Sliding window for reception deduplication: a redelivery of the same radio
# packet (broker QoS retry, reconnect replay) arrives seconds to minutes
# later.  Outside the window an equal mesh_packet_id is a *new* packet — the
# id is random 32-bit and wraps around over a node's lifetime.
DEDUP_WINDOW_SECONDS = 600.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS packets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    topic TEXT,
    from_node_id INTEGER,
    to_node_id INTEGER,
    portnum INTEGER,
    portnum_name TEXT,
    gateway_id TEXT,
    gateway_node_id INTEGER,
    channel_id TEXT,
    mesh_packet_id INTEGER,
    rssi INTEGER,
    snr REAL,
    hop_limit INTEGER,
    hop_start INTEGER,
    rx_time INTEGER,
    via_mqtt INTEGER,
    next_hop INTEGER,
    relay_node INTEGER,
    reply_id INTEGER,
    emoji INTEGER,
    payload_length INTEGER,
    raw_payload BLOB,
    processed INTEGER DEFAULT 1,
    message_type TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS nodes (
    node_id INTEGER PRIMARY KEY,
    hex_id TEXT,
    long_name TEXT,
    short_name TEXT,
    hw_model TEXT,
    role TEXT,
    latitude REAL,
    longitude REAL,
    altitude REAL,
    position_ts REAL,
    first_seen REAL,
    last_seen REAL,
    packet_count INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_packets_time ON packets(timestamp);
CREATE INDEX IF NOT EXISTS idx_packets_port_time ON packets(portnum_name, timestamp);
CREATE INDEX IF NOT EXISTS idx_packets_from_time ON packets(from_node_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_packets_gateway_time ON packets(gateway_node_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_packets_direct ON packets(gateway_node_id, hop_start, hop_limit);
CREATE INDEX IF NOT EXISTS idx_packets_dedup ON packets(from_node_id, mesh_packet_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_nodes_last_seen ON nodes(last_seen);
"""

_INSERT_PACKET = """
INSERT INTO packets (
    timestamp, topic, from_node_id, to_node_id, portnum, portnum_name,
    gateway_id, gateway_node_id, channel_id, mesh_packet_id, rssi, snr,
    hop_limit, hop_start, rx_time, via_mqtt, next_hop, relay_node,
    reply_id, emoji,
    payload_length, raw_payload, processed, message_type, error
) VALUES (
    :timestamp, :topic, :from_node_id, :to_node_id, :portnum, :portnum_name,
    :gateway_id, :gateway_node_id, :channel_id, :mesh_packet_id, :rssi, :snr,
    :hop_limit, :hop_start, :rx_time, :via_mqtt, :next_hop, :relay_node,
    :reply_id, :emoji, :payload_length, :raw_payload, :processed, :message_type,
    :error
)
"""

_UPSERT_NODE_BASE = """
INSERT INTO nodes (node_id, hex_id, first_seen, last_seen, packet_count)
VALUES (:node_id, :hex_id, :ts, :ts, 1)
ON CONFLICT(node_id) DO UPDATE SET
    last_seen = MAX(COALESCE(nodes.last_seen, 0), excluded.last_seen),
    packet_count = COALESCE(nodes.packet_count, 0) + 1,
    hex_id = COALESCE(NULLIF(excluded.hex_id, ''), nodes.hex_id)
"""

_UPDATE_NODE_INFO = """
UPDATE nodes SET
    hex_id = COALESCE(:hex_id, hex_id),
    long_name = COALESCE(:long_name, long_name),
    short_name = COALESCE(:short_name, short_name),
    hw_model = COALESCE(:hw_model, hw_model),
    role = COALESCE(:role, role)
WHERE node_id = :node_id
"""

_UPDATE_NODE_POSITION = """
UPDATE nodes SET
    latitude = :latitude, longitude = :longitude, altitude = :altitude,
    position_ts = :position_ts
WHERE node_id = :node_id
"""


def _connect(db_file: str) -> sqlite3.Connection:
    path = Path(db_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA temp_store=MEMORY")
    return conn


def init(db_file: str) -> None:
    """Create the schema (idempotent)."""
    global _initialized_for
    with _lock:
        conn = _connect(db_file)
        try:
            conn.executescript(SCHEMA)
            _migrate(conn)
            conn.commit()
        finally:
            conn.close()
        if _initialized_for != db_file:
            _initialized_for = db_file
            logger.info("Database ready: %s", db_file)


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the first cut of the schema."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(packets)")}
    for column, decl in (
        ("message_type", "TEXT"),
        ("reply_id", "INTEGER"),
        ("emoji", "INTEGER"),
    ):
        if column not in existing:
            conn.execute(f"ALTER TABLE packets ADD COLUMN {column} {decl}")

    # Legacy duplicates — the same reception delivered twice by the broker —
    # predate the sliding-window check in insert_packet.  The sweep is
    # idempotent: a second run finds nothing to delete (rows without a packet
    # id are intentionally never matched).
    conn.execute(
        """
        DELETE FROM packets
        WHERE mesh_packet_id IS NOT NULL
          AND EXISTS (
            SELECT 1 FROM packets AS earlier
            WHERE earlier.id < packets.id
              AND earlier.from_node_id = packets.from_node_id
              AND earlier.mesh_packet_id = packets.mesh_packet_id
              AND earlier.gateway_id IS packets.gateway_id
              AND ABS(earlier.timestamp - packets.timestamp) <= ?
          )
        """,
        (DEDUP_WINDOW_SECONDS,),
    )


def ensure_ready(settings: Settings) -> None:
    init(settings.db_file)


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def _is_duplicate_reception(conn: sqlite3.Connection, packet: DecodedPacket) -> bool:
    """True when this (sender, packet id, gateway) was already heard nearby in time."""
    if packet.mesh_packet_id is None or not packet.from_node_id:
        return False  # without a packet id there is nothing to match on
    ts = packet.timestamp
    row = conn.execute(
        """
        SELECT 1 FROM packets
        WHERE from_node_id = :from_id
          AND mesh_packet_id = :packet_id
          AND gateway_id IS :gateway
          AND timestamp BETWEEN :lo AND :hi
        LIMIT 1
        """,
        {
            "from_id": packet.from_node_id,
            "packet_id": packet.mesh_packet_id,
            "gateway": packet.gateway_id,
            "lo": ts - DEDUP_WINDOW_SECONDS,
            "hi": ts + DEDUP_WINDOW_SECONDS,
        },
    ).fetchone()
    return row is not None


def insert_packet(db_file: str, packet: DecodedPacket) -> bool:
    """Persist one decoded packet plus any node metadata it carried.

    Returns False for a duplicate reception: the same mesh packet heard again
    through the same gateway inside DEDUP_WINDOW_SECONDS (broker redelivery,
    reconnect replay).  Storing it would inflate node counters and edge
    strength; a different gateway is a different reception and is kept.
    """
    with _lock:
        conn = _connect(db_file)
        try:
            if _is_duplicate_reception(conn, packet):
                return False
            row = packet.to_row()
            row["processed"] = 1 if packet.processed else 0
            row["via_mqtt"] = None if packet.via_mqtt is None else int(packet.via_mqtt)
            conn.execute(_INSERT_PACKET, row)

            ts = packet.timestamp
            if packet.from_node_id:
                conn.execute(
                    _UPSERT_NODE_BASE,
                    {"node_id": packet.from_node_id, "hex_id": None, "ts": ts},
                )
            if packet.gateway_node_id:
                conn.execute(
                    _UPSERT_NODE_BASE,
                    {
                        "node_id": packet.gateway_node_id,
                        "hex_id": packet.gateway_id,
                        "ts": ts,
                    },
                )

            if packet.node_info and packet.node_info.get("node_id"):
                info = packet.node_info
                conn.execute(
                    _UPDATE_NODE_INFO,
                    {
                        "node_id": info["node_id"],
                        "hex_id": info.get("hex_id"),
                        "long_name": info.get("long_name"),
                        "short_name": info.get("short_name"),
                        "hw_model": info.get("hw_model"),
                        "role": info.get("role"),
                    },
                )

            if packet.position and packet.from_node_id:
                conn.execute(
                    _UPDATE_NODE_POSITION,
                    {
                        "node_id": packet.from_node_id,
                        "latitude": packet.position["latitude"],
                        "longitude": packet.position["longitude"],
                        "altitude": packet.position.get("altitude"),
                        "position_ts": packet.position["timestamp"],
                    },
                )

            conn.commit()
            return True
        finally:
            conn.close()


def prune(db_file: str, retention_hours: int) -> int:
    """Drop packets older than the retention window.  0 disables pruning.

    Stale node rows (last seen before the cutoff) go with their packets, and
    the survivors' ``packet_count`` is recounted: the counter feeds edge
    weights, so it must track the stored rows — a node counts once per row as
    the sender and once as the gateway, mirroring ``_UPSERT_NODE_BASE``.

    ``raw_payload`` travels with its packet and is never trimmed separately:
    the chat renders messages straight from the BLOB, so history size is
    bounded by the window (``retention_hours = 0`` keeps everything — an
    explicit choice to let the database grow).
    """
    if retention_hours <= 0:
        return 0
    cutoff = time.time() - retention_hours * 3600
    with _lock:
        conn = _connect(db_file)
        try:
            cur = conn.execute("DELETE FROM packets WHERE timestamp < ?", (cutoff,))
            deleted = cur.rowcount or 0
            conn.execute("DELETE FROM nodes WHERE last_seen < ?", (cutoff,))
            if deleted:
                conn.execute(
                    """
                    UPDATE nodes SET packet_count =
                        (SELECT COUNT(*) FROM packets AS p
                          WHERE p.from_node_id = nodes.node_id)
                      + (SELECT COUNT(*) FROM packets AS p
                          WHERE p.gateway_node_id = nodes.node_id)
                    """
                )
            conn.commit()
            if deleted:
                logger.info("Pruned %s packets older than %sh", deleted, retention_hours)
            return deleted
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Reads used by the graph builder / UI
# ---------------------------------------------------------------------------

def query(db_file: str, sql: str, params: Sequence[Any] | tuple = ()) -> list[dict]:
    conn = _connect(db_file)
    try:
        rows = conn.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# Ids per IN (...) batch: well below SQLite's variable limit (32766 by
# default today, but some builds still ship the classic 999).
NODE_LOOKUP_CHUNK = 500


def node_lookup(db_file: str, node_ids: list[int]) -> dict[int, dict[str, Any]]:
    """Bulk name / coordinate lookup for a set of node ids (chunked by IN-size)."""
    if not node_ids:
        return {}
    found: dict[int, dict[str, Any]] = {}
    for start in range(0, len(node_ids), NODE_LOOKUP_CHUNK):
        chunk = node_ids[start : start + NODE_LOOKUP_CHUNK]
        placeholders = ",".join("?" for _ in chunk)
        rows = query(
            db_file,
            f"""
            SELECT node_id, hex_id, long_name, short_name, hw_model, role,
                   latitude, longitude, altitude, position_ts, last_seen
            FROM nodes WHERE node_id IN ({placeholders})
            """,
            chunk,
        )
        found.update({r["node_id"]: r for r in rows})
    return found


def distinct_channels(db_file: str, since: float) -> list[str]:
    rows = query(
        db_file,
        """
        SELECT DISTINCT channel_id FROM packets
        WHERE timestamp >= ? AND channel_id IS NOT NULL AND channel_id != ''
        ORDER BY channel_id
        """,
        (since,),
    )
    return [r["channel_id"] for r in rows]


def gateway_ids(db_file: str, since: float) -> set[int]:
    rows = query(
        db_file,
        """
        SELECT DISTINCT gateway_node_id FROM packets
        WHERE timestamp >= ? AND gateway_node_id IS NOT NULL
        """,
        (since,),
    )
    return {r["gateway_node_id"] for r in rows if r["gateway_node_id"] is not None}


def stats(db_file: str) -> dict[str, Any]:
    """Rough table sizes for the status panel."""
    rows = query(
        db_file,
        """
        SELECT
            (SELECT COUNT(*) FROM packets) AS packets,
            (SELECT COUNT(*) FROM nodes) AS nodes,
            (SELECT COUNT(*) FROM packets
              WHERE portnum_name = 'TRACEROUTE_APP') AS traceroutes,
            (SELECT MAX(timestamp) FROM packets) AS last_packet_ts
        """,
    )
    return rows[0] if rows else {}
