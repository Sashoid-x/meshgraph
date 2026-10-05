"""Sliding message-rate bookkeeping, dedup counting and pruning in the worker."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from meshgraph import mqtt_worker, store
from meshgraph.mqtt_worker import RATE_WINDOW_SECONDS, CaptureWorker

from .conftest import make_packet


def test_rate_counts_messages_per_minute(settings_store):
    worker = CaptureWorker(settings_store)
    worker._started_monotonic = 0.0

    with worker._lock:
        for _ in range(30):
            worker._note_message(100.0)  # burst, all at once
        snapshot = worker._rate_snapshot(400.0)  # five minutes later

    # Nothing in the last minute; 30 messages over five minutes = 6/min.
    assert snapshot["rate_1m"] == 0.0
    assert snapshot["rate_5m"] == 6.0


def test_rate_uses_shorter_span_before_the_window_elapses(settings_store):
    worker = CaptureWorker(settings_store)
    worker._started_monotonic = 0.0

    with worker._lock:
        for _ in range(5):
            worker._note_message(10.0)
        snapshot = worker._rate_snapshot(10.0)

    # The worker has been up for 10 s: 5 messages there = 30/min, not 5/min.
    assert snapshot["rate_1m"] == 30.0
    assert snapshot["rate_5m"] == 30.0


def test_old_arrivals_are_pruned_from_the_window(settings_store):
    worker = CaptureWorker(settings_store)
    worker._started_monotonic = 0.0

    with worker._lock:
        worker._note_message(0.0)
        worker._note_message(RATE_WINDOW_SECONDS + 10.0)

        # The stale stamp is gone; only the fresh one still counts (0.2/min).
        assert list(worker._message_times) == [RATE_WINDOW_SECONDS + 10.0]
        snapshot = worker._rate_snapshot(RATE_WINDOW_SECONDS + 10.0)
        assert snapshot["rate_5m"] == 0.2


def test_rate_survives_seconds_without_traffic(settings_store):
    worker = CaptureWorker(settings_store)
    worker._started_monotonic = -200.0  # the windows are fully elapsed

    with worker._lock:
        for _ in range(12):
            worker._note_message(60.0)
        # A quiet minute later the 1-minute rate is empty, the 5-minute one lives.
        snapshot = worker._rate_snapshot(200.0)

    assert snapshot["rate_1m"] == 0.0
    assert snapshot["rate_5m"] == 2.4  # 12 messages over the 5-minute window


def test_status_reports_rate_fields(settings_store, tmp_path):
    settings_store.update(db_file=str(tmp_path / "rate.db"))
    worker = CaptureWorker(settings_store)

    stats = worker.status()["stats"]

    assert stats["rate_1m"] == 0.0
    assert stats["rate_5m"] == 0.0
    assert stats["messages"] == 0


# ---------------------------------------------------------------------------
# Reception deduplication counter (G-P0-2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stored", "deduplicated"),
    [(True, 0), (False, 1)],
    ids=["fresh", "duplicate"],
)
def test_on_message_counts_deduplicated_receptions(
    settings_store, tmp_path, monkeypatch, stored, deduplicated
):
    settings_store.update(db_file=str(tmp_path / "worker.db"))
    worker = CaptureWorker(settings_store)
    packet = make_packet(mesh_packet_id=7)
    monkeypatch.setattr(
        mqtt_worker, "decode_message", lambda topic, payload, keys: packet
    )
    monkeypatch.setattr(mqtt_worker.store, "insert_packet", lambda db, p: stored)
    msg = SimpleNamespace(topic="msh/US/2/e/LongFast/!00000001", payload=b"x")

    worker._on_message(None, None, msg)

    stats = worker.status()["stats"]
    assert stats["messages"] == 1
    assert stats["decoded"] == 1
    assert stats["deduplicated"] == deduplicated


# ---------------------------------------------------------------------------
# Retention through the worker (G-P0-1)
# ---------------------------------------------------------------------------


def _seed_two_generations(db_file: str) -> None:
    store.init(db_file)
    now = time.time()
    store.insert_packet(
        db_file,
        make_packet(
            timestamp=now - 48 * 3600, from_node_id=5, gateway_node_id=5, mesh_packet_id=1
        ),
    )
    store.insert_packet(
        db_file,
        make_packet(
            timestamp=now - 3600, from_node_id=5, gateway_node_id=5, mesh_packet_id=2
        ),
    )


def test_saved_retention_really_cleans_the_database(settings_store, tmp_path):
    db_file = str(tmp_path / "prune.db")
    _seed_two_generations(db_file)

    settings_store.update(db_file=db_file, retention_hours=0)
    worker = CaptureWorker(settings_store)
    worker._prune_if_due(settings_store.get())  # "0 — хранить бессрочно"
    rows = store.query(db_file, "SELECT COUNT(*) AS c FROM packets")
    assert rows[0]["c"] == 2

    settings_store.update(retention_hours=24)  # what pressing Save in the UI does
    worker._prune_if_due(settings_store.get())

    assert store.query(db_file, "SELECT COUNT(*) AS c FROM packets")[0]["c"] == 1
    counts = store.query(db_file, "SELECT packet_count AS c FROM nodes WHERE node_id = 5")
    assert counts[0]["c"] == 2  # the survivor counts as sender + gateway


def test_prune_failure_lands_in_last_error(settings_store, tmp_path, monkeypatch):
    settings_store.update(db_file=str(tmp_path / "x.db"), retention_hours=1)
    worker = CaptureWorker(settings_store)

    def boom(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(mqtt_worker.store, "prune", boom)
    worker._prune_if_due(settings_store.get())

    stats = worker.status()["stats"]
    assert stats["errors"] == 1
    assert "prune failed" in stats["last_error"]
