"""Schema migrations, the chat columns, retention and reception dedup."""

from __future__ import annotations

import sqlite3
import time

from meshgraph import store
from meshgraph.store import DEDUP_WINDOW_SECONDS

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


# ---------------------------------------------------------------------------
# Retention (store.prune, G-P0-1)
# ---------------------------------------------------------------------------


def _count(db_file: str, table: str = "packets") -> int:
    return store.query(db_file, f"SELECT COUNT(*) AS c FROM {table}")[0]["c"]


def test_prune_keeps_everything_when_retention_is_off(settings):
    now = time.time()
    for node in (5, 6):
        store.insert_packet(
            settings.db_file,
            make_packet(
                timestamp=now - 48 * 3600,
                from_node_id=node,
                gateway_node_id=node,
                mesh_packet_id=node,
            ),
        )

    deleted = store.prune(settings.db_file, 0)

    assert deleted == 0
    assert _count(settings.db_file) == 2
    assert _count(settings.db_file, "nodes") == 2


def test_prune_drops_old_rows_stale_nodes_and_recounts(settings):
    now = time.time()
    # D has not been heard since forever: packet row and node row both go.
    store.insert_packet(
        settings.db_file,
        make_packet(
            timestamp=now - 100 * 3600, from_node_id=7, gateway_node_id=7, mesh_packet_id=10
        ),
    )
    # A has an old row and a fresh one: the old row goes, the node stays with
    # a packet_count recounted from the surviving rows.
    store.insert_packet(
        settings.db_file,
        make_packet(
            timestamp=now - 100 * 3600, from_node_id=5, gateway_node_id=5, mesh_packet_id=11
        ),
    )
    store.insert_packet(
        settings.db_file,
        make_packet(
            timestamp=now - 3600, from_node_id=5, gateway_node_id=5, mesh_packet_id=12
        ),
    )

    deleted = store.prune(settings.db_file, 24)

    assert deleted == 2
    assert [
        row["from_node_id"]
        for row in store.query(settings.db_file, "SELECT from_node_id FROM packets")
    ] == [5]

    counts = {
        row["node_id"]: row["packet_count"]
        for row in store.query(settings.db_file, "SELECT node_id, packet_count FROM nodes")
    }
    assert 7 not in counts          # unseen since before the cutoff — removed
    assert counts[5] == 2           # one surviving row: sender + gateway mention


def test_prune_is_idempotent(settings):
    store.insert_packet(
        settings.db_file,
        make_packet(
            timestamp=time.time() - 48 * 3600,
            from_node_id=5,
            gateway_node_id=5,
            mesh_packet_id=1,
        ),
    )

    assert store.prune(settings.db_file, 24) == 1
    assert store.prune(settings.db_file, 24) == 0
    assert _count(settings.db_file) == 0


# ---------------------------------------------------------------------------
# Reception deduplication (G-P0-2)
# ---------------------------------------------------------------------------


def test_duplicate_reception_is_skipped(settings):
    first = make_packet(
        from_node_id=5, gateway_node_id=5, mesh_packet_id=42, timestamp=1_000_000.0
    )
    redelivery = make_packet(
        from_node_id=5, gateway_node_id=5, mesh_packet_id=42, timestamp=1_000_000.0 + 30
    )

    assert store.insert_packet(settings.db_file, first) is True
    assert store.insert_packet(settings.db_file, redelivery) is False

    assert _count(settings.db_file) == 1
    # The skipped duplicate must not inflate the node counter either.
    counts = store.query(
        settings.db_file, "SELECT packet_count AS c FROM nodes WHERE node_id = 5"
    )
    assert counts[0]["c"] == 2  # sender + gateway of the single stored row


def test_same_packet_via_another_gateway_is_kept(settings):
    assert (
        store.insert_packet(
            settings.db_file,
            make_packet(
                from_node_id=5, gateway_node_id=5, mesh_packet_id=42, timestamp=1_000_000.0
            ),
        )
        is True
    )
    assert (
        store.insert_packet(
            settings.db_file,
            make_packet(
                from_node_id=5, gateway_node_id=9, mesh_packet_id=42, timestamp=1_000_000.0
            ),
        )
        is True
    )  # a different gateway heard it — a genuinely different reception

    assert _count(settings.db_file) == 2


def test_packet_id_reuse_outside_the_window_is_kept(settings):
    base = dict(from_node_id=5, gateway_node_id=5, mesh_packet_id=42)
    assert (
        store.insert_packet(
            settings.db_file, make_packet(timestamp=1_000_000.0, **base)
        )
        is True
    )
    assert (
        store.insert_packet(
            settings.db_file,
            make_packet(timestamp=1_000_000.0 + DEDUP_WINDOW_SECONDS + 1, **base),
        )
        is True
    )  # mesh_packet_id is random 32-bit: outside the window it is a new packet

    assert _count(settings.db_file) == 2


def test_packets_without_ids_never_deduplicate(settings):
    for _ in range(2):
        assert (
            store.insert_packet(
                settings.db_file,
                make_packet(
                    from_node_id=5, gateway_node_id=5, mesh_packet_id=None,
                    timestamp=1_000_000.0,
                ),
            )
            is True
        )

    assert _count(settings.db_file) == 2


def test_migration_drops_historical_duplicates_and_is_idempotent(db_file):
    store.init(db_file)
    conn = sqlite3.connect(db_file)
    try:
        # Two legacy rows of the same reception (30 s apart) plus a legitimate
        # id reuse an hour later — only the second duplicate must survive.
        for ts in (1_000_000.0, 1_000_030.0, 1_000_000.0 + 3600):
            conn.execute(
                "INSERT INTO packets (timestamp, from_node_id, gateway_id,"
                " mesh_packet_id) VALUES (?, 5, '!00000005', 42)",
                (ts,),
            )
        conn.commit()
    finally:
        conn.close()

    store.init(db_file)  # migration sweep
    store.init(db_file)  # again — must be a no-op

    rows = store.query(db_file, "SELECT timestamp FROM packets ORDER BY timestamp")
    assert [row["timestamp"] for row in rows] == [1_000_000.0, 1_000_000.0 + 3600]


# ---------------------------------------------------------------------------
# node_lookup chunking (G-P1-6)
# ---------------------------------------------------------------------------


def test_node_lookup_chunks_large_id_lists(settings, monkeypatch):
    known = list(range(0x1000, 0x100A))  # ten named nodes out of five thousand ids
    now = time.time()
    conn = sqlite3.connect(settings.db_file)
    try:
        for node_id in known:
            conn.execute(
                "INSERT INTO nodes (node_id, long_name, first_seen, last_seen,"
                " packet_count) VALUES (?, ?, ?, ?, 0)",
                (node_id, f"node-{node_id}", now, now),
            )
        conn.commit()
    finally:
        conn.close()

    ids = list(range(1, 5001))
    found = store.node_lookup(settings.db_file, ids)

    assert sorted(found) == known
    assert found[known[0]]["long_name"] == "node-4096"

    # The answer must not depend on how the ids were split into batches.
    monkeypatch.setattr(store, "NODE_LOOKUP_CHUNK", 7)
    assert store.node_lookup(settings.db_file, ids) == found
    assert store.node_lookup(settings.db_file, []) == {}
