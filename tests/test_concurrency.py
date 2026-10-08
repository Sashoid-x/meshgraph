"""Concurrent access: parallel readers/writers and the worker's stop() race.

The production process runs the MQTT capture threads and the Flask threads
against one SQLite file at the same time (G-P2-5).
"""

from __future__ import annotations

import threading
import time

from meshgraph import mqtt_worker, store

from .conftest import make_packet
from .test_mqtt_worker import fake_factory


def test_parallel_inserts_and_queries_stay_consistent(settings):
    """Параллельные записи и чтения: ни исключений, ни потерянных строк."""
    errors: list[BaseException] = []
    stop_reading = threading.Event()
    writers, per_writer = 3, 60

    def writer(w: int) -> None:
        try:
            for i in range(per_writer):
                store.insert_packet(
                    settings.db_file,
                    make_packet(
                        from_node_id=1 + i % 7,
                        gateway_node_id=9,
                        mesh_packet_id=w * 10_000 + i,  # уникальны — без дедупа
                        timestamp=1_700_000_000.0 + i,
                    ),
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def reader() -> None:
        try:
            while not stop_reading.is_set():
                store.query(
                    settings.db_file, "SELECT COUNT(*) AS n FROM packets"
                )
                store.node_lookup(settings.db_file, [1, 2, 3, 4])
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    readers = [threading.Thread(target=reader, daemon=True) for _ in range(2)]
    writer_threads = [
        threading.Thread(target=writer, args=(w,)) for w in range(writers)
    ]
    for thread in readers:
        thread.start()
    for thread in writer_threads:
        thread.start()
    for thread in writer_threads:
        thread.join(timeout=60)
    stop_reading.set()
    for thread in readers:
        thread.join(timeout=10)

    assert not errors
    rows = store.query(settings.db_file, "SELECT COUNT(*) AS n FROM packets")
    assert rows[0]["n"] == writers * per_writer


def test_stop_is_safe_while_replacing_clients(settings_store, tmp_path):
    """Гонка stop() ↔ _build_link: ни исключений, ни поднятых потоков."""
    settings_store.update(db_file=str(tmp_path / "race.db"))
    store.ensure_ready(settings_store.get())
    worker = mqtt_worker.CaptureWorker(
        settings_store, client_factory=fake_factory()
    )
    worker.start()

    def hammer() -> None:
        settings = settings_store.get()
        pid = settings.active_connection
        for _ in range(30):
            worker._build_link(pid, settings)

    replacing = threading.Thread(target=hammer, daemon=True)
    replacing.start()
    time.sleep(0.1)
    try:
        worker.stop()  # не должен упасть посреди замены клиента
    finally:
        replacing.join(timeout=30)
        worker.stop()  # убрать клиента, созданного после основного stop()

    assert not replacing.is_alive()
    assert worker._links == {}
    alive = [
        t.name
        for t in threading.enumerate()
        if t.name.startswith("meshgraph-mqtt")
    ]
    assert alive == []
