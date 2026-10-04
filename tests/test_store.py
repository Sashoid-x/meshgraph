"""Schema migrations and the chat columns of the packets table."""

from __future__ import annotations

import sqlite3

from meshgraph import store

from .conftest import make_packet

# The packets table as it existed before reply/emoji were captured: enough to
# prove ALTER TABLE adds the chat columns on a live database.
PACKETS_WITHOUT_CHAT_COLUMNS = """
CREATE TABLE packets (
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
    payload_length INTEGER,
    raw_payload BLOB,
    processed INTEGER DEFAULT 1,
    message_type TEXT,
    error TEXT
);
"""


def _columns(db_file: str) -> set[str]:
    conn = sqlite3.connect(db_file)
    try:
        return {row[1] for row in conn.execute("PRAGMA table_info(packets)")}
    finally:
        conn.close()


def test_migrate_adds_chat_columns_to_an_existing_database(db_file):
    conn = sqlite3.connect(db_file)
    conn.execute(PACKETS_WITHOUT_CHAT_COLUMNS)
    conn.commit()
    conn.close()

    store.init(db_file)

    assert {"reply_id", "emoji"} <= _columns(db_file)
    # Нodes and indexes are (re)created after the migration ran.
    conn = sqlite3.connect(db_file)
    tables = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    indexes = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        )
    }
    conn.close()
    assert "nodes" in tables
    assert "idx_packets_port_time" in indexes


def test_migrate_is_idempotent(db_file):
    store.init(db_file)
    before = _columns(db_file)

    store.init(db_file)

    assert _columns(db_file) == before


def test_reply_and_emoji_round_trip(db_file):
    store.init(db_file)
    store.insert_packet(
        db_file,
        make_packet(
            portnum_name="TEXT_MESSAGE_APP",
            mesh_packet_id=4242,
            reply_id=1111,
            emoji=1,
            raw_payload=b"hi",
        ),
    )

    rows = store.query(
        db_file, "SELECT reply_id, emoji FROM packets WHERE mesh_packet_id = ?", [4242]
    )

    assert list(rows[0].values()) == [1111, 1]


def test_unset_chat_columns_stay_null(db_file):
    store.init(db_file)
    store.insert_packet(
        db_file,
        make_packet(portnum_name="TRACEROUTE_APP", mesh_packet_id=1, raw_payload=b"\x08"),
    )

    rows = store.query(
        db_file, "SELECT reply_id, emoji FROM packets WHERE mesh_packet_id = ?", [1]
    )

    assert list(rows[0].values()) == [None, None]
