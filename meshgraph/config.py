"""Settings: load, validate and persist the MQTT / graph configuration.

The settings file is plain YAML (``config.yaml`` by default, overridable with
``MESHGRAPH_CONFIG``).  Everything the settings dialog edits lives here so that
the capture worker and the web UI always read the same source of truth.
"""

from __future__ import annotations

import logging
import os
import base64
import threading
from copy import deepcopy
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Callable

import yaml

from .crypto import MAX_PSK_BYTES, MESHTASTIC_DEFAULT_PSK

logger = logging.getLogger(__name__)

# Default Meshtastic LongFast channel key (base64).  Traceroute packets on the
# default channel are encrypted with it, so a graph built without any key would
# silently stay empty.  Equal to the firmware's `defaultpsk`, i.e. what the
# one-byte PSK alias `AQ==` expands to.
DEFAULT_CHANNEL_KEY = base64.b64encode(MESHTASTIC_DEFAULT_PSK).decode()

GRAPH_MODES = ("traceroute", "rssi", "combined")

_ENV_PREFIX = "MESHGRAPH_"


@dataclass
class Settings:
    # --- MQTT broker -----------------------------------------------------
    mqtt_broker_address: str = "127.0.0.1"
    mqtt_port: int = 1883
    mqtt_username: str = ""
    mqtt_password: str = ""
    mqtt_topic_prefix: str = "msh"
    mqtt_topic_suffix: str = "/+/+/+/#"
    mqtt_client_id: str = ""
    # TLS — brokers like mqtt.onemesh.ru:8883 require it.
    mqtt_tls: bool = False
    # Skip certificate verification: self-signed brokers or broken CA stores.
    mqtt_tls_insecure: bool = False
    # Comma-separated list of base64 channel keys; each is tried in order.
    decryption_keys: str = DEFAULT_CHANNEL_KEY

    # --- Storage / retention --------------------------------------------
    db_file: str = "data/graph.db"
    retention_hours: int = 0  # 0 == keep forever
    graph_packet_limit: int = 5000

    # --- Web UI ----------------------------------------------------------
    web_host: str = "127.0.0.1"
    web_port: int = 5010

    # --- Defaults for the graph page -------------------------------------
    default_graph_mode: str = "traceroute"
    default_hours: int = 24

    # Derived -----------------------------------------------------------------

    @property
    def keys(self) -> list[str]:
        """Decryption keys, split on commas and stripped."""
        return [k.strip() for k in self.decryption_keys.split(",") if k.strip()]

    @property
    def subscribe_topic(self) -> str:
        return f"{self.mqtt_topic_prefix}{self.mqtt_topic_suffix}"

    def masked(self) -> dict[str, Any]:
        """Serializable copy with the broker password replaced by a mask."""
        data = asdict(self)
        if data["mqtt_password"]:
            data["mqtt_password"] = "••••••••"
            data["mqtt_password_set"] = True
        else:
            data["mqtt_password_set"] = False
        return data


def config_path() -> Path:
    """Location of the YAML settings file."""
    override = os.environ.get(f"{_ENV_PREFIX}CONFIG")
    if override:
        return Path(override).expanduser()
    return Path.cwd() / "config.yaml"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate(settings: Settings) -> list[str]:
    """Return a list of human-readable problems; empty means valid."""
    errors: list[str] = []

    if not str(settings.mqtt_broker_address).strip():
        errors.append("Broker address is required.")

    try:
        port = int(settings.mqtt_port)
        if not 1 <= port <= 65535:
            errors.append("MQTT port must be between 1 and 65535.")
    except (TypeError, ValueError):
        errors.append("MQTT port must be a number.")

    if settings.default_graph_mode not in GRAPH_MODES:
        errors.append(f"Graph mode must be one of: {', '.join(GRAPH_MODES)}.")

    try:
        hours = int(settings.default_hours)
        if not 1 <= hours <= 720:
            errors.append("Default time period must be between 1 and 720 hours.")
    except (TypeError, ValueError):
        errors.append("Default time period must be a number.")

    try:
        retention = int(settings.retention_hours)
        if retention < 0:
            errors.append("Retention must be 0 (never) or a positive number of hours.")
    except (TypeError, ValueError):
        errors.append("Retention must be a number.")

    if settings.mqtt_topic_prefix is None or not str(settings.mqtt_topic_prefix).strip():
        errors.append("Topic prefix is required (e.g. `msh`).")

    # Meshtastic publishes to `<root>/<version>/<type>/<channel>/<node-id>` — a
    # subscription without a wildcard names one exact topic and will never
    # match anything.
    subscribe = f"{settings.mqtt_topic_prefix or ''}{settings.mqtt_topic_suffix or ''}"
    if "#" not in subscribe and "+" not in subscribe:
        errors.append(
            "Topic template needs a wildcard, otherwise nothing matches: "
            f"got `{subscribe}`, e.g. prefix `msh` + template `/RU/SAR/#`."
        )

    # Channel keys must be base64 the firmware could actually store (PSK is
    # capped at 32 bytes).  Expanding the one-byte aliases and padding short
    # keys is crypto.normalize_psk's job — see the firmware's Channels::getKey.
    for key in settings.keys:
        try:
            decoded = base64.b64decode(key, validate=True)
        except Exception:
            errors.append(f"Channel key `{key}` is not valid base64.")
            continue
        if not decoded:
            errors.append(f"Channel key `{key}` is empty.")
        elif len(decoded) == 1 and decoded[0] == 0:
            errors.append(
                f"Channel key `{key}` switches encryption off; remove it instead."
            )
        elif len(decoded) > MAX_PSK_BYTES:
            errors.append(
                f"Channel key `{key}` must be at most {MAX_PSK_BYTES} bytes, "
                f"got {len(decoded)}."
            )

    return errors


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class SettingsStore:
    """Thread-safe settings holder with change notification."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or config_path()
        self._lock = threading.RLock()
        self._settings = Settings()
        self._listeners: list[Callable[[Settings], None]] = []
        self.load()

    # -- persistence -------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> Settings:
        with self._lock:
            data: dict[str, Any] = {}
            if self._path.exists():
                try:
                    loaded = yaml.safe_load(self._path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        data = loaded
                    elif loaded is not None:
                        # The user must learn their settings were ignored.
                        logger.warning(
                            "Ignoring settings file %s: expected a mapping", self._path
                        )
                except yaml.YAMLError as exc:
                    # A corrupt file must not stop the service; fall back to
                    # defaults and let the user fix it from the settings dialog.
                    logger.warning(
                        "Ignoring corrupt settings file %s: %s", self._path, exc
                    )
                    data = {}

            known = {f.name for f in fields(Settings)}
            self._settings = Settings(**{k: v for k, v in data.items() if k in known})
            return deepcopy(self._settings)

    def save(self, settings: Settings) -> None:
        """Validate, persist and publish new settings."""
        problems = validate(settings)
        if problems:
            raise ValueError("; ".join(problems))

        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = asdict(settings)
            self._path.write_text(
                yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
                encoding="utf-8",
            )
            self._settings = deepcopy(settings)

        self._notify(deepcopy(settings))

    # -- access ------------------------------------------------------------

    def get(self) -> Settings:
        with self._lock:
            return deepcopy(self._settings)

    def update(self, **changes: Any) -> Settings:
        """Merge partial changes, validate, persist and notify."""
        current = self.get()
        known = {f.name for f in fields(Settings)}
        unknown = set(changes) - known
        if unknown:
            raise ValueError(f"Unknown settings: {', '.join(sorted(unknown))}")

        for key, value in changes.items():
            if key == "mqtt_password" and value == "••••••••":
                # The dialog echoes back a mask when the field was left alone.
                continue
            setattr(current, key, value)

        # Coerce the numeric columns; HTML forms always submit strings.
        for name in (
            "mqtt_port",
            "retention_hours",
            "graph_packet_limit",
            "web_port",
            "default_hours",
        ):
            try:
                setattr(current, name, int(getattr(current, name)))
            except (TypeError, ValueError):
                pass

        # Checkboxes arrive as JSON booleans, a hand-written config.yaml or a
        # curl call may as well send "true"/"false".
        for name in ("mqtt_tls", "mqtt_tls_insecure"):
            value = getattr(current, name)
            if isinstance(value, str):
                setattr(current, name, value.strip().lower() in ("1", "true", "yes", "on"))
            else:
                setattr(current, name, bool(value))

        self.save(current)
        return current

    # -- change notification ------------------------------------------------

    def subscribe(self, callback: Callable[[Settings], None]) -> None:
        with self._lock:
            self._listeners.append(callback)

    def _notify(self, settings: Settings) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for callback in listeners:
            try:
                callback(settings)
            except Exception:  # noqa: BLE001 - a listener must not break saving
                pass


_store: SettingsStore | None = None
_store_lock = threading.Lock()


def get_settings_store() -> SettingsStore:
    """Process-wide settings singleton."""
    global _store
    with _store_lock:
        if _store is None:
            _store = SettingsStore()
        return _store
