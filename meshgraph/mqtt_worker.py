"""The MQTT capture worker: subscribe, decode, persist, report status.

Runs as a background thread inside the web process so that pressing *Save* in
the settings dialog can reconnect to a new broker without a restart.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from typing import Any, Callable

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from . import graph, store
from .config import Settings, SettingsStore
from .decoder import decode_message, sanitize_for_log

logger = logging.getLogger(__name__)

# How far back the sliding message rate looks (the /api/status "rate_5m").
RATE_WINDOW_SECONDS = 300.0

# How often the capture loop re-checks the retention setting (G-P0-1).
PRUNE_INTERVAL_SECONDS = 3600.0

# Raw messages buffered between the paho callback and the store thread: at
# the documented 15-20 msg/s that is nearly a minute of headroom.  When the
# queue is full the OLDEST item is shed — the freshest packets matter most
# (G-P1-2).
INBOX_MAXSIZE = 1000


def _rc_value(reason_code: Any) -> int:
    """Normalise paho reason codes (int, ReasonCode, or None) to an int."""
    if reason_code is None:
        return 0
    if isinstance(reason_code, int):
        return reason_code
    value = getattr(reason_code, "value", None)
    if value is not None:
        try:
            return int(value)
        except (TypeError, ValueError):
            pass
    try:
        return int(reason_code)
    except (TypeError, ValueError):
        return 1


class CaptureWorker:
    """Owns the paho client and keeps it in sync with the settings store."""

    def __init__(
        self,
        settings_store: SettingsStore,
        client_factory: Callable[..., mqtt.Client] | None = None,
    ) -> None:
        self._settings_store = settings_store
        self._lock = threading.RLock()
        self._client: mqtt.Client | None = None
        self._settings: Settings = settings_store.get()
        self._running = False
        self._thread: threading.Thread | None = None
        self._store_thread: threading.Thread | None = None
        # Test seam: a fake factory records lifecycle calls without touching
        # the network (G-P1-1).
        self._client_factory = client_factory or mqtt.Client
        self._inbox: "queue.Queue[tuple[str, bytes, Settings]]" = queue.Queue(
            maxsize=INBOX_MAXSIZE
        )

        self._stats: dict[str, Any] = {
            "messages": 0,
            "decoded": 0,
            "deduplicated": 0,
            "dropped": 0,
            "dropped_queue": 0,
            "decrypt_failed": 0,
            "errors": 0,
            "reconnects": 0,
            "last_message_ts": None,
            "last_error": None,
        }
        # Arrival stamps for the sliding rate, guarded by ``self._lock``.
        self._message_times: deque[float] = deque()
        self._connected = False
        self._subscribed_topic: str | None = None
        self._started_at: float | None = None
        self._started_monotonic: float | None = None

        settings_store.subscribe(self._on_settings_changed)

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._started_at = time.time()
            self._started_monotonic = time.monotonic()
        self._thread = threading.Thread(
            target=self._run, name="meshgraph-mqtt", daemon=True
        )
        self._store_thread = threading.Thread(
            target=self._drain_inbox, name="meshgraph-mqtt-store", daemon=True
        )
        self._thread.start()
        self._store_thread.start()

    def stop(self) -> None:
        with self._lock:
            self._running = False
            client, self._client = self._client, None
        if client is not None:
            try:
                client.disconnect()
                client.loop_stop()
            except Exception:  # noqa: BLE001
                pass
        # Join both threads: the supervisor must not outlive stop(), and the
        # store thread finishes draining the inbox first (G-P1-1, G-P1-2).
        threads = (self._thread, self._store_thread)
        self._thread = None
        self._store_thread = None
        for thread in threads:
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=5.0)

    def _run(self) -> None:
        """Rebuild the client whenever settings change, keeping one alive."""
        seen_signature: str | None = None
        next_prune = 0.0  # honour the retention setting from the very first tick
        while True:
            with self._lock:
                if not self._running:
                    return
                settings = self._settings
            signature = self._signature(settings)

            if signature != seen_signature:
                seen_signature = signature
                self._replace_client(settings)

            now = time.time()
            if now >= next_prune:
                next_prune = now + PRUNE_INTERVAL_SECONDS
                self._prune_if_due(settings)

            time.sleep(0.5)

    def _drain_inbox(self) -> None:
        """Decode and store queued messages (the ``meshgraph-mqtt-store`` thread).

        Decode, decryption and SQLite writes must not block paho's network
        loop (G-P1-2): the callback only enqueues.  On shutdown whatever is
        already queued is processed before the thread exits.
        """
        while True:
            try:
                topic, payload, settings = self._inbox.get(timeout=0.2)
            except queue.Empty:
                with self._lock:
                    stopping = not self._running
                if stopping:
                    return
                continue
            try:
                self._process_message(topic, payload, settings)
            finally:
                self._inbox.task_done()

    def _prune_if_due(self, settings: Settings) -> None:
        """Honour ``retention_hours`` (0 keeps everything), called hourly from ``_run``.

        Housekeeping must never take the capture down: failures land in
        ``last_error`` like any other worker error.
        """
        if not settings.retention_hours:
            return
        try:
            store.prune(settings.db_file, settings.retention_hours)
        except Exception as exc:  # noqa: BLE001
            self._record_error(f"prune failed: {sanitize_for_log(exc)}")

    @staticmethod
    def _signature(settings: Settings) -> str:
        return "|".join(
            [
                str(settings.mqtt_broker_address),
                str(settings.mqtt_port),
                str(settings.mqtt_username),
                str(settings.mqtt_password),
                str(settings.mqtt_topic_prefix),
                str(settings.mqtt_topic_suffix),
                str(settings.mqtt_client_id),
                str(settings.mqtt_tls),
                str(settings.mqtt_tls_insecure),
                str(settings.decryption_keys),
                str(settings.db_file),
            ]
        )

    def _replace_client(self, settings: Settings) -> None:
        with self._lock:
            old, self._client = self._client, None
        if old is not None:
            try:
                old.disconnect()
                old.loop_stop()
            except Exception:  # noqa: BLE001
                pass
            logger.info("Released previous MQTT client")

        try:
            store.ensure_ready(settings)
        except Exception as exc:  # noqa: BLE001
            self._record_error(f"database init failed: {exc}")
            return

        client = self._client_factory(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=settings.mqtt_client_id or None,
            protocol=mqtt.MQTTv311,
            reconnect_on_failure=True,
        )
        if settings.mqtt_username:
            client.username_pw_set(
                settings.mqtt_username, settings.mqtt_password or None
            )
        if settings.mqtt_tls:
            try:
                # Default CA bundle; `mqtt_tls_insecure` downgrades to no
                # verification for self-signed brokers.
                client.tls_set()
                if settings.mqtt_tls_insecure:
                    client.tls_insecure_set(True)
            except Exception as exc:  # noqa: BLE001 - missing CA store, etc.
                self._record_error(f"TLS setup failed: {sanitize_for_log(exc)}")
                return

        client.on_connect = self._on_connect
        client.on_connect_fail = self._on_connect_fail
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message

        client.reconnect_delay_set(min_delay=1, max_delay=60)

        with self._lock:
            if not self._running:
                return
            self._client = client
            self._settings = settings
            self._subscribed_topic = None

        logger.info(
            "Connecting to %s://%s:%s (topic %s)",
            "mqtts" if settings.mqtt_tls else "mqtt",
            sanitize_for_log(settings.mqtt_broker_address),
            settings.mqtt_port,
            sanitize_for_log(settings.subscribe_topic),
        )
        try:
            # Held across connect/loop_start: a stop() landing between the
            # "am I running" check and loop_start() used to leave an orphaned
            # client reconnecting forever (G-P1-1).
            with self._lock:
                if not self._running:
                    return
                client.connect_async(
                    settings.mqtt_broker_address,
                    int(settings.mqtt_port),
                    keepalive=60,
                )
                client.loop_start()
        except Exception as exc:  # noqa: BLE001
            self._record_error(f"connect failed: {sanitize_for_log(exc)}")

    def _on_settings_changed(self, settings: Settings) -> None:
        """Called from the settings store when the dialog saves."""
        with self._lock:
            self._settings = settings
        graph.invalidate_cache()

    # -- paho callbacks ----------------------------------------------------

    def _on_connect(
        self, client: mqtt.Client, userdata: Any, flags: Any, reason_code: Any, properties: Any = None
    ) -> None:
        if _rc_value(reason_code) == 0:
            with self._lock:
                self._connected = True
                self._stats["last_error"] = None
                settings = self._settings
            topic = settings.subscribe_topic
            client.subscribe(topic)
            with self._lock:
                self._subscribed_topic = topic
            logger.info("Connected to MQTT broker, subscribed to %s", topic)
        else:
            with self._lock:
                self._connected = False
            self._record_error(f"CONNACK refused: {reason_code}")

    def _on_connect_fail(self, client: mqtt.Client, userdata: Any) -> None:
        """paho calls this on every failed TCP/TLS attempt during retry."""
        with self._lock:
            settings = self._settings
        self._record_error(
            f"cannot reach {sanitize_for_log(settings.mqtt_broker_address)}:"
            f"{settings.mqtt_port} ({'mqtts' if settings.mqtt_tls else 'mqtt'}) "
            "— check host, port and TLS"
        )

    def _on_disconnect(
        self, client: mqtt.Client, userdata: Any, disconnect_flags: Any, reason_code: Any, properties: Any = None
    ) -> None:
        with self._lock:
            self._connected = False
            self._subscribed_topic = None
            if _rc_value(reason_code) != 0 and self._running:
                self._stats["reconnects"] += 1
        if _rc_value(reason_code) != 0:
            logger.warning("MQTT disconnected unexpectedly: %s", reason_code)

    def _on_message(
        self, client: mqtt.Client, userdata: Any, msg: mqtt.MQTTMessage
    ) -> None:
        """paho network-loop callback: count the arrival and hand it over.

        Only a non-blocking enqueue happens here — decode, decryption and
        SQLite writes live on the store thread, so a slow disk can no longer
        stall keepalives (G-P1-2).
        """
        with self._lock:
            settings = self._settings
            self._stats["messages"] += 1
            self._stats["last_message_ts"] = time.time()
            self._note_message(time.monotonic())

        try:
            self._inbox.put_nowait((msg.topic, msg.payload, settings))
        except queue.Full:
            with self._lock:
                self._stats["dropped_queue"] += 1
            # Shed the oldest item: fresher packets are worth more.
            try:
                self._inbox.get_nowait()
                self._inbox.task_done()
            except queue.Empty:
                pass
            try:
                self._inbox.put_nowait((msg.topic, msg.payload, settings))
            except queue.Full:
                pass  # a racing producer won; this packet is dropped

    def _process_message(self, topic: str, payload: bytes, settings: Settings) -> None:
        """Decode and persist one queued message (store thread)."""
        try:
            packet = decode_message(topic, payload, settings.keys)
        except Exception as exc:  # noqa: BLE001
            self._record_error(f"decode failed: {sanitize_for_log(exc)}")
            return

        if packet is None:
            with self._lock:
                self._stats["dropped"] += 1
            return

        if packet.error and "undecryptable" in (packet.error or ""):
            with self._lock:
                self._stats["decrypt_failed"] += 1

        try:
            stored = store.insert_packet(settings.db_file, packet)
            with self._lock:
                self._stats["decoded"] += 1
                if not stored:
                    self._stats["deduplicated"] += 1
        except Exception as exc:  # noqa: BLE001
            self._record_error(f"store failed: {sanitize_for_log(exc)}")

    # -- helpers -----------------------------------------------------------

    def _note_message(self, now: float) -> None:
        """Remember an arrival for the sliding rate; callers hold ``_lock``."""
        times = self._message_times
        times.append(now)
        while times and now - times[0] > RATE_WINDOW_SECONDS:
            times.popleft()

    def _rate_snapshot(self, now: float) -> dict[str, float]:
        """Messages per minute over the last minute and the last five.

        Right after a start the windows have not elapsed yet, so the shorter
        uptime is used as the denominator — otherwise a burst in the first
        seconds would be reported as a rate over a full minute.
        """
        times = self._message_times
        while times and now - times[0] > RATE_WINDOW_SECONDS:
            times.popleft()

        def per_minute(window: float) -> float:
            span = window
            if self._started_monotonic is not None:
                span = min(window, max(now - self._started_monotonic, 1.0))
            recent = sum(1 for t in times if now - t <= span)
            return round(recent * 60.0 / span, 1)

        return {
            "rate_1m": per_minute(60.0),
            "rate_5m": per_minute(RATE_WINDOW_SECONDS),
        }

    def _record_error(self, message: str) -> None:
        """Store the failure; repeated identical retries are not new errors.

        paho retries every few seconds, so counting each attempt would inflate
        ``errors`` while leaving the user none the wiser about the cause.
        """
        with self._lock:
            repeated = self._stats["last_error"] == message
            if not repeated:
                self._stats["errors"] += 1
            self._stats["last_error"] = message
        if not repeated:
            logger.warning(message)

    # -- introspection -----------------------------------------------------

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connected

    def status(self) -> dict[str, Any]:
        """Snapshot for ``/api/status``."""
        settings = self._settings_store.get()
        with self._lock:
            stats = dict(self._stats)
            stats.update(self._rate_snapshot(time.monotonic()))
            connected = self._connected
            topic = self._subscribed_topic
            started_at = self._started_at

        return {
            "connected": connected,
            "broker": f"{settings.mqtt_broker_address}:{settings.mqtt_port}",
            "tls": bool(settings.mqtt_tls),
            "tls_insecure": bool(settings.mqtt_tls_insecure),
            "subscribe_topic": topic or settings.subscribe_topic,
            "keys_configured": len(settings.keys),
            "stats": stats,
            "uptime_seconds": round(time.time() - started_at, 1)
            if started_at
            else None,
            "db": _safe_db_stats(settings.db_file),
        }


def _safe_db_stats(db_file: str) -> dict[str, Any]:
    try:
        return store.stats(db_file)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def main() -> None:  # pragma: no cover - manual capture-only entry point
    """Run the capture loop without the web UI (``python -m meshgraph.mqtt_worker``)."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
    )
    from .config import get_settings_store

    store_ref = get_settings_store()
    worker = CaptureWorker(store_ref)
    worker.start()
    logger.info("Capture-only mode; press Ctrl+C to stop")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        worker.stop()


if __name__ == "__main__":  # pragma: no cover
    main()
