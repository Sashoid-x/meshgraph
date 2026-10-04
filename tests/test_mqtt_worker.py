"""Sliding message-rate bookkeeping in the capture worker."""

from __future__ import annotations

from meshgraph.mqtt_worker import RATE_WINDOW_SECONDS, CaptureWorker


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
