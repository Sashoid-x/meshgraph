"""Message rate, dedup, pruning and the MQTT lifecycle of the capture worker."""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from meshgraph import mqtt_worker, store
from meshgraph.decoder import DecodedPacket
from meshgraph.mqtt_worker import RATE_WINDOW_SECONDS, CaptureWorker

from .conftest import make_packet


# ---------------------------------------------------------------------------
# Fakes for the client seam (G-P1-1)
# ---------------------------------------------------------------------------


class FakeClient:
    """Stands in for paho's Client: records lifecycle calls, no sockets."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.block_connect = False
        self.connect_entered = threading.Event()
        self.connect_release = threading.Event()
        self.connect_error: Exception | None = None
        self.tls_error: Exception | None = None
        self.disconnected = False
        self.loop_running = False
        self.subscriptions: list[str] = []

    # -- configuration paho applies before connecting ----------------------
    def username_pw_set(self, *args, **kwargs):  # noqa: D102
        pass

    def tls_set(self, *args, **kwargs):  # noqa: D102
        if self.tls_error is not None:
            raise self.tls_error

    def tls_insecure_set(self, *args, **kwargs):  # noqa: D102
        pass

    def reconnect_delay_set(self, **kwargs):  # noqa: D102
        pass

    def subscribe(self, topic):  # noqa: D102
        self.subscriptions.append(topic)

    # -- lifecycle ----------------------------------------------------------
    def connect_async(self, host, port, keepalive=60):  # noqa: D102
        if self.connect_error is not None:
            raise self.connect_error
        if self.block_connect:
            self.connect_entered.set()
            self.connect_release.wait(timeout=5.0)

    def loop_start(self):  # noqa: D102
        self.loop_running = True

    def disconnect(self):  # noqa: D102
        self.disconnected = True

    def loop_stop(self):  # noqa: D102
        self.loop_running = False


def fake_factory(made=None, *, block=False, tls_error=None, connect_error=None):
    """Build a CaptureWorker factory handing out configured FakeClients."""
    def factory(**kwargs):
        client = FakeClient(**kwargs)
        client.block_connect = block
        client.tls_error = tls_error
        client.connect_error = connect_error
        if made is not None:
            made.append(client)
        return client
    return factory


def _worker(settings_store, factory=None) -> CaptureWorker:
    return CaptureWorker(settings_store, client_factory=factory or fake_factory())


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
def test_process_message_counts_deduplicated_receptions(
    settings, settings_store, monkeypatch, stored, deduplicated
):
    settings_store.update(db_file=settings.db_file)
    worker = _worker(settings_store)
    packet = make_packet(mesh_packet_id=7)
    monkeypatch.setattr(
        mqtt_worker, "decode_message", lambda topic, payload, keys: packet
    )
    monkeypatch.setattr(mqtt_worker.store, "insert_packet", lambda db, p: stored)

    worker._process_message("t", b"x", settings)

    stats = worker.status()["stats"]
    assert stats["decoded"] == 1
    assert stats["deduplicated"] == deduplicated


def test_on_message_counts_and_enqueues_without_decoding(
    settings_store, tmp_path, monkeypatch
):
    """The paho callback only counts and enqueues — decode happens elsewhere."""
    settings_store.update(db_file=str(tmp_path / "q.db"))
    worker = _worker(settings_store)
    decode_calls = []
    monkeypatch.setattr(
        mqtt_worker, "decode_message", lambda *args: decode_calls.append(args)
    )

    worker._on_message(None, None, SimpleNamespace(topic="t", payload=b"x"))

    stats = worker.status()["stats"]
    assert stats["messages"] == 1
    assert stats["last_message_ts"] is not None
    assert worker._inbox.qsize() == 1
    assert decode_calls == []  # ничего не декодируется в callback-потоке


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


# ---------------------------------------------------------------------------
# MQTT lifecycle (G-P1-1)
# ---------------------------------------------------------------------------


def test_stop_during_connect_leaves_no_orphan_client(settings_store, tmp_path):
    """stop(), попавшее между проверкой и loop_start(), не должно оставить
    переподключающегося клиента (гонка из плана)."""
    settings_store.update(db_file=str(tmp_path / "w.db"))
    made: list[FakeClient] = []
    worker = _worker(settings_store, fake_factory(made, block=True))
    with worker._lock:
        worker._running = True

    replacer = threading.Thread(
        target=worker._replace_client, args=(settings_store.get(),)
    )
    replacer.start()
    deadline = time.time() + 5
    while not made and time.time() < deadline:
        time.sleep(0.01)
    assert made, "client factory was never called"
    assert made[0].connect_entered.wait(timeout=5), "connect_async was never reached"

    stopper = threading.Thread(target=worker.stop)
    stopper.start()
    time.sleep(0.1)  # stop() упирается в лок, который держит connect
    made[0].connect_release.set()
    replacer.join(timeout=5)
    stopper.join(timeout=5)

    client = made[0]
    assert not replacer.is_alive() and not stopper.is_alive()
    assert client.disconnected
    assert client.loop_running is False  # loop_stop сработал ПОСЛЕ loop_start
    assert worker._client is None


def test_replace_client_closes_the_previous_client(settings_store, tmp_path):
    settings_store.update(db_file=str(tmp_path / "w.db"))
    made: list[FakeClient] = []
    worker = _worker(settings_store, fake_factory(made))
    with worker._lock:
        worker._running = True
    settings = settings_store.get()

    worker._replace_client(settings)
    worker._replace_client(settings)

    assert len(made) == 2
    assert made[0].disconnected and made[0].loop_running is False
    assert made[1].loop_running is True  # новый клиент уже работает
    assert worker._client is made[1]


def test_record_error_counts_only_new_failures(settings_store, tmp_path):
    settings_store.update(db_file=str(tmp_path / "w.db"))
    worker = _worker(settings_store)

    worker._record_error("boom")
    worker._record_error("boom")  # paho повторяет попытки каждые секунды
    worker._record_error("other")

    stats = worker.status()["stats"]
    assert stats["errors"] == 2
    assert stats["last_error"] == "other"


def test_on_connect_marks_connected_and_subscribes(settings_store, tmp_path):
    settings_store.update(db_file=str(tmp_path / "w.db"))
    worker = _worker(settings_store)
    client = FakeClient()

    worker._on_connect(client, None, None, 0)

    assert worker.connected is True
    assert worker.status()["connected"] is True
    assert client.subscriptions == [settings_store.get().subscribe_topic]
    assert worker.status()["stats"]["last_error"] is None


def test_connack_refusal_is_reported(settings_store, tmp_path):
    settings_store.update(db_file=str(tmp_path / "w.db"))
    worker = _worker(settings_store)

    worker._on_connect(FakeClient(), None, None, 5)

    stats = worker.status()["stats"]
    assert worker.connected is False
    assert stats["errors"] == 1
    assert "CONNACK refused" in stats["last_error"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, 0),
        (0, 0),
        (7, 7),  # старый paho: обычный int
        (SimpleNamespace(value=9), 9),  # новый paho: объект с .value
        (SimpleNamespace(value="junk"), 1),  # не разобрано → общая ошибка
        (object(), 1),
    ],
)
def test_rc_value_normalises_three_paho_generations(raw, expected):
    assert mqtt_worker._rc_value(raw) == expected


def test_tls_failure_lands_in_last_error(settings_store, tmp_path):
    settings_store.update(db_file=str(tmp_path / "w.db"))
    # Правим локальную копию: хранилище в тесте не валидирует TLS-подпись.
    candidate = replace(settings_store.get(), mqtt_tls=True)
    worker = _worker(settings_store, fake_factory(tls_error=OSError("no CA store")))
    with worker._lock:
        worker._running = True

    worker._replace_client(candidate)

    stats = worker.status()["stats"]
    assert stats["errors"] == 1
    assert stats["last_error"].startswith("TLS setup failed")
    assert worker._client is None


def test_connect_failure_lands_in_last_error(settings_store, tmp_path):
    settings_store.update(db_file=str(tmp_path / "w.db"))
    worker = _worker(settings_store, fake_factory(connect_error=OSError("net down")))
    with worker._lock:
        worker._running = True

    worker._replace_client(settings_store.get())

    stats = worker.status()["stats"]
    assert stats["errors"] == 1
    assert stats["last_error"].startswith("connect failed")


def test_on_connect_fail_reports_unreachable_broker(settings_store, tmp_path):
    settings_store.update(db_file=str(tmp_path / "w.db"))
    worker = _worker(settings_store)

    worker._on_connect_fail(FakeClient(), None)
    worker._on_connect_fail(FakeClient(), None)  # повтор той же попытки

    stats = worker.status()["stats"]
    assert stats["errors"] == 1  # пах каждые секунды — не новая ошибка
    assert "cannot reach" in stats["last_error"]


def test_stop_joins_both_threads(settings_store, tmp_path):
    settings_store.update(db_file=str(tmp_path / "w.db"))
    worker = _worker(settings_store)
    worker.start()
    run_thread, store_thread = worker._thread, worker._store_thread
    assert run_thread.is_alive() and store_thread.is_alive()

    worker.stop()

    assert worker._thread is None and worker._store_thread is None
    assert not run_thread.is_alive() and not store_thread.is_alive()


def test_start_stop_cycles_do_not_leak_threads(settings_store, tmp_path):
    settings_store.update(db_file=str(tmp_path / "w.db"))
    worker = _worker(settings_store)
    baseline = {t.ident for t in _meshgraph_threads()}

    for _ in range(3):
        worker.start()
        time.sleep(0.05)
        worker.stop()

    deadline = time.time() + 5
    while time.time() < deadline:
        leaked = [t for t in _meshgraph_threads() if t.ident not in baseline]
        if not leaked:
            break
        time.sleep(0.05)
    assert [t for t in _meshgraph_threads() if t.ident not in baseline] == []


def _meshgraph_threads() -> list[threading.Thread]:
    return [
        t
        for t in threading.enumerate()
        if t.name.startswith("meshgraph-") and t.is_alive()
    ]


# ---------------------------------------------------------------------------
# Store queue (G-P1-2)
# ---------------------------------------------------------------------------


def test_decode_and_store_run_off_the_callback_thread(
    settings_store, tmp_path, monkeypatch
):
    settings_store.update(db_file=str(tmp_path / "q.db"))
    store.ensure_ready(settings_store.get())  # в проде это делает create_app
    seen: dict[str, str] = {}

    def fake_decode(topic, payload, keys):
        seen["thread"] = threading.current_thread().name
        return make_packet(mesh_packet_id=1)

    monkeypatch.setattr(mqtt_worker, "decode_message", fake_decode)
    worker = _worker(settings_store)
    worker.start()
    try:
        caller = threading.current_thread().name
        worker._on_message(None, None, SimpleNamespace(topic="t", payload=b"x"))

        deadline = time.time() + 5
        while time.time() < deadline:
            if worker.status()["stats"]["decoded"] >= 1:
                break
            time.sleep(0.01)
        assert worker.status()["stats"]["decoded"] == 1
        assert seen["thread"] == "meshgraph-mqtt-store"
        assert seen["thread"] != caller
    finally:
        worker.stop()


def test_full_inbox_drops_the_oldest_without_blocking(
    settings_store, tmp_path, monkeypatch
):
    monkeypatch.setattr(mqtt_worker, "INBOX_MAXSIZE", 1)
    settings_store.update(db_file=str(tmp_path / "q.db"))
    worker = _worker(settings_store)  # потребителя нет: очередь переполнится
    started = time.time()

    worker._on_message(None, None, SimpleNamespace(topic="first", payload=b"1"))
    worker._on_message(None, None, SimpleNamespace(topic="second", payload=b"2"))

    assert time.time() - started < 1.0  # put_nowait не блокировал продюсера
    stats = worker.status()["stats"]
    assert stats["messages"] == 2
    assert stats["dropped_queue"] == 1
    topic, _payload, _settings = worker._inbox.get_nowait()
    assert topic == "second"  # вытеснен старый пакет, свежий остался


def test_stop_drains_the_inbox(settings_store, tmp_path, monkeypatch):
    settings_store.update(db_file=str(tmp_path / "q.db"))
    store.ensure_ready(settings_store.get())
    worker = _worker(settings_store)
    monkeypatch.setattr(
        mqtt_worker,
        "decode_message",
        lambda topic, payload, keys: make_packet(mesh_packet_id=9),
    )

    worker._on_message(None, None, SimpleNamespace(topic="t", payload=b"x"))
    worker.start()
    worker.stop()  # сразу: очередь обязана быть дообработана, ничего не потеряно

    assert worker.status()["stats"]["decoded"] == 1


def test_process_message_counts_dropped_when_decode_yields_nothing(
    settings, settings_store, monkeypatch
):
    settings_store.update(db_file=settings.db_file)
    worker = _worker(settings_store)
    monkeypatch.setattr(mqtt_worker, "decode_message", lambda *args: None)

    worker._process_message("t", b"x", settings)

    stats = worker.status()["stats"]
    assert stats["dropped"] == 1
    assert stats["decoded"] == 0


def test_process_message_counts_undecryptable_packets(
    settings, settings_store, monkeypatch
):
    settings_store.update(db_file=settings.db_file)
    worker = _worker(settings_store)
    undecryptable = DecodedPacket(
        timestamp=time.time(),
        topic="msh/US/2/e/LongFast/!00000001",
        from_node_id=1,
        portnum_name="TEXT_MESSAGE_APP",
        error="payload undecryptable (no key)",
    )
    monkeypatch.setattr(mqtt_worker, "decode_message", lambda *args: undecryptable)

    worker._process_message("t", b"x", settings)

    stats = worker.status()["stats"]
    assert stats["decrypt_failed"] == 1
    assert stats["decoded"] == 1  # пакет сохранён, счётчик лишь метит причину


def test_settings_change_invalidates_the_graph_cache(settings_store, tmp_path):
    """G-P2-5: сохранение настроек обязано сбрасывать кэш графа."""
    from meshgraph import graph

    settings_store.update(db_file=str(tmp_path / "cache.db"))
    store.ensure_ready(settings_store.get())  # схемы ещё нет — создаём
    graph.invalidate_cache()
    graph.build_graph(settings_store.get(), mode="traceroute", use_cache=True)
    assert graph._cache  # что-то закэшировано

    worker = _worker(settings_store)
    worker._on_settings_changed(settings_store.get())

    assert not graph._cache  # диалог настроек снёс кэш


def test_safe_db_stats_reports_counts_and_survives_a_broken_path(
    settings, tmp_path
):
    # Обычный путь — грубые счётчики таблиц.
    good = mqtt_worker._safe_db_stats(settings.db_file)
    assert {"packets", "nodes", "traceroutes"} <= set(good)

    # Нечитаемая база (каталог вместо файла) — ошибка, а не исключение.
    bad = mqtt_worker._safe_db_stats(str(tmp_path))
    assert "error" in bad
