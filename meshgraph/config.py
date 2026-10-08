"""Settings: load, validate and persist the MQTT / graph configuration.

The settings file is plain YAML (``config.yaml`` by default, overridable with
``MESHGRAPH_CONFIG``).  Everything the settings dialog edits lives here so that
the capture worker and the web UI always read the same source of truth.
"""

from __future__ import annotations

import logging
import os
import base64
import re
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

# Fields that belong to one connection (broker, topic, credentials) and
# travel together into a profile; storage/web/UI preferences stay global.
# Keep in sync with CONNECTION_FIELDS in app.js.
PROFILE_FIELDS = (
    "mqtt_broker_address",
    "mqtt_port",
    "mqtt_username",
    "mqtt_password",
    "mqtt_topic_prefix",
    "mqtt_topic_suffix",
    "mqtt_client_id",
    "mqtt_tls",
    "mqtt_tls_insecure",
    "decryption_keys",
)


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

    # --- Connection profiles -----------------------------------------------
    # Registry of saved connections: each profile owns its broker/topic/
    # credentials AND its own database file, so data from one server can
    # never end up mixed into another server's history.  The flat MQTT
    # fields above are the working copy of the ACTIVE profile — every
    # existing consumer (worker, web, chat) reads them and stays unaware of
    # this list (see _reconcile / _retarget_active).
    connections: list[dict[str, Any]] = field(default_factory=list)
    active_connection: str = ""
    connection_name: str = ""

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
        # Profiles are exposed as an index only: GET /api/settings must not
        # leak the other servers' passwords or channel keys.
        data["connections"] = [
            {
                "id": p.get("id"),
                "name": p.get("name"),
                "broker": p.get("mqtt_broker_address"),
                "port": p.get("mqtt_port"),
                "db_file": p.get("db_file"),
            }
            for p in self.connections
            if isinstance(p, dict)
        ]
        if data["mqtt_password"]:
            data["mqtt_password"] = "••••••••"
            data["mqtt_password_set"] = True
        else:
            data["mqtt_password_set"] = False
        return data


# ---------------------------------------------------------------------------
# Connection profile helpers
# ---------------------------------------------------------------------------

def _slug(text: Any) -> str:
    """Lowercase ASCII slug — connection ids double as database file names."""
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-") or "server"


def _connection_identity(
    broker: Any, port: Any, prefix: Any, suffix: Any
) -> tuple[str, int, str]:
    """The (broker, port, subscribe topic) triple identifying a data stream.

    The topic is prefix + suffix — exactly what the worker subscribes to:
    another topic on the same broker delivers another network's packets, so
    it deserves a connection and a database of its own.  The broker part is
    case-insensitive (it is DNS), topics are not (MQTT topics are
    case-sensitive).  A changed triple means a different server.
    """
    try:
        port_number = int(port)
    except (TypeError, ValueError):
        port_number = -1
    topic = f"{prefix or ''}{suffix or ''}"
    return (str(broker or "").strip().lower(), port_number, topic)


def _profile_identity(profile: dict[str, Any]) -> tuple[str, int, str]:
    return _connection_identity(
        profile.get("mqtt_broker_address"),
        profile.get("mqtt_port"),
        profile.get("mqtt_topic_prefix"),
        profile.get("mqtt_topic_suffix"),
    )


def _default_connection_name(broker: Any, port: Any, topic: Any) -> str:
    return f"{broker}:{port} ({topic})"


def _derive_connection_id(broker: Any, topic: Any, taken: list[str]) -> str:
    """A stable id derived from broker + subscribe topic, unique among ``taken``."""
    base = f"{_slug(broker)}-{_slug(topic)}"
    candidate = base
    suffix = 2
    while candidate in taken:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def _derived_db_file(pid: str, current_db_file: str) -> str:
    """A fresh database next to the current connection's file, named after ``pid``.

    Landing next to the current file keeps databases together (``data/`` in
    production, a tmp dir in tests), and deriving from the profile id means
    recreating a deleted connection re-attaches its old file when it is still
    there — the database belongs to the server, not to the profile instance.
    """
    return str(Path(current_db_file).parent / f"{pid}.db")


def _profile_from_flat(
    settings: Settings, pid: str, name: str, db_file: str
) -> dict[str, Any]:
    """Snapshot the flat working copy into a new profile."""
    profile: dict[str, Any] = {"id": pid, "name": name}
    for fname in PROFILE_FIELDS:
        profile[fname] = getattr(settings, fname)
    profile["db_file"] = db_file
    return profile


def _profile_snapshot(profile: dict[str, Any]) -> dict[str, Any]:
    """The flat-field view of a profile (what it looked like before an edit)."""
    snapshot = {name: profile.get(name) for name in PROFILE_FIELDS}
    snapshot["connection_name"] = profile.get("name")
    snapshot["db_file"] = profile.get("db_file")
    return snapshot


def _apply_profile(settings: Settings, profile: dict[str, Any]) -> None:
    """Point the flat working copy at the profile's connection."""
    for fname in PROFILE_FIELDS:
        if fname in profile:
            setattr(settings, fname, profile[fname])
    if profile.get("db_file"):
        settings.db_file = str(profile["db_file"])
    settings.connection_name = str(profile.get("name") or "")


def _absorb_flat(settings: Settings, profile: dict[str, Any]) -> None:
    """Store the flat working copy back into the profile (in-place edit)."""
    for fname in PROFILE_FIELDS:
        profile[fname] = getattr(settings, fname)
    profile["db_file"] = settings.db_file
    if settings.connection_name:
        profile["name"] = settings.connection_name
    else:
        settings.connection_name = str(profile.get("name") or "")


def _flat_connection_snapshot(settings: Settings) -> dict[str, Any]:
    """The pre-edit state an identity change is diffed against."""
    snapshot = {name: getattr(settings, name) for name in PROFILE_FIELDS}
    snapshot["connection_name"] = settings.connection_name
    snapshot["db_file"] = settings.db_file
    return snapshot


def _coerce_flat(settings: Settings) -> None:
    """Normalize numbers and checkbox flags after a merge (forms send strings)."""
    for name in (
        "mqtt_port",
        "retention_hours",
        "graph_packet_limit",
        "web_port",
        "default_hours",
    ):
        try:
            setattr(settings, name, int(getattr(settings, name)))
        except (TypeError, ValueError):
            pass

    # Checkboxes arrive as JSON booleans, a hand-written config.yaml or a
    # curl call may as well send "true"/"false".
    for name in ("mqtt_tls", "mqtt_tls_insecure"):
        value = getattr(settings, name)
        if isinstance(value, str):
            setattr(settings, name, value.strip().lower() in ("1", "true", "yes", "on"))
        else:
            setattr(settings, name, bool(value))


def _normalize_profiles(raw: Any) -> list[dict[str, Any]]:
    """Coerce a YAML-loaded registry into profiles with unique ids and a db file."""
    if not isinstance(raw, list):
        return []
    profiles = [dict(p) for p in raw if isinstance(p, dict)]
    seen: list[str] = []
    for profile in profiles:
        pid = str(profile.get("id") or "").strip()
        if not pid or pid in seen:
            pid = _derive_connection_id(
                profile.get("mqtt_broker_address"),
                f"{profile.get('mqtt_topic_prefix') or ''}"
                f"{profile.get('mqtt_topic_suffix') or ''}",
                seen,
            )
        profile["id"] = pid
        seen.append(pid)
        try:
            profile["mqtt_port"] = int(profile.get("mqtt_port"))
        except (TypeError, ValueError):
            pass
        if not str(profile.get("db_file") or "").strip():
            profile["db_file"] = f"data/{pid}.db"
        if not str(profile.get("name") or "").strip():
            profile["name"] = _default_connection_name(
                profile.get("mqtt_broker_address"),
                profile.get("mqtt_port"),
                f"{profile.get('mqtt_topic_prefix') or ''}"
                f"{profile.get('mqtt_topic_suffix') or ''}",
            )
    return profiles


def _retarget_active(settings: Settings, previous: dict[str, Any]) -> None:
    """The flat identity changed: activate a matching profile or spawn one.

    ``previous`` holds the pre-edit flat values, so only fields the edit
    really changed travel onto a profile we retarget (its own credentials
    stay its own).  A brand-new server gets a brand-new profile with a
    brand-new database file — flat ``db_file`` is never carried over, it
    still points at the old connection's data.
    """
    identity = _connection_identity(
        settings.mqtt_broker_address,
        settings.mqtt_port,
        settings.mqtt_topic_prefix,
        settings.mqtt_topic_suffix,
    )
    profiles = settings.connections
    match = next((p for p in profiles if _profile_identity(p) == identity), None)

    if match is not None:
        for fname in PROFILE_FIELDS:
            if getattr(settings, fname) != previous.get(fname):
                match[fname] = getattr(settings, fname)
        new_name = str(settings.connection_name or "").strip()
        if new_name and new_name != previous.get("connection_name"):
            match["name"] = new_name
        settings.active_connection = str(match["id"])
        _apply_profile(settings, match)
        return

    taken = [str(p.get("id") or "") for p in profiles]
    pid = _derive_connection_id(
        settings.mqtt_broker_address, settings.subscribe_topic, taken
    )
    name = str(settings.connection_name or "").strip()
    if not name or name == previous.get("connection_name"):
        name = _default_connection_name(
            settings.mqtt_broker_address, settings.mqtt_port, settings.subscribe_topic
        )
    profile = _profile_from_flat(
        settings,
        pid,
        name,
        _derived_db_file(pid, str(previous.get("db_file") or settings.db_file)),
    )
    profiles.append(profile)
    settings.active_connection = pid
    settings.db_file = profile["db_file"]
    settings.connection_name = name


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

    # Connection registry: ids and database files must be unique and the
    # active profile must exist — otherwise a switch would be a coin toss.
    if settings.connections:
        seen_ids: set[str] = set()
        seen_dbs: set[str] = set()
        for profile in settings.connections:
            if not isinstance(profile, dict):
                errors.append("Each connection must be a mapping.")
                continue
            pid = str(profile.get("id") or "").strip() or "?"
            if pid == "?":
                errors.append("Connection id is required.")
            elif pid in seen_ids:
                errors.append(f"Duplicate connection id: {pid}.")
            else:
                seen_ids.add(pid)
            if not str(profile.get("mqtt_broker_address") or "").strip():
                errors.append(f"Connection `{pid}` needs a broker address.")
            try:
                port = int(profile.get("mqtt_port"))
                if not 1 <= port <= 65535:
                    errors.append(f"Connection `{pid}`: port must be between 1 and 65535.")
            except (TypeError, ValueError):
                errors.append(f"Connection `{pid}`: port must be a number.")
            db_file = str(profile.get("db_file") or "").strip()
            if not db_file:
                errors.append(f"Connection `{pid}` needs a database file.")
            elif db_file in seen_dbs:
                errors.append(f"Two connections share the database file: `{db_file}`.")
            else:
                seen_dbs.add(db_file)
        active = str(settings.active_connection or "").strip()
        if active and active not in seen_ids:
            errors.append(f"Active connection `{active}` is not in the list.")

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
            # A pre-connections config file becomes the first profile (its
            # database file is kept as-is); with a registry present the flat
            # fields win and an edited broker/topic spawns a new connection.
            self._reconcile(self._settings)
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

        # Switching the active connection comes first: connection fields sent
        # in the same call then apply on top of the profile switched to.
        active = changes.pop("active_connection", None)
        if active is not None:
            if current.connections and str(active) != current.active_connection:
                self._switch_to(current, str(active))
            else:
                current.active_connection = str(active)

        # What the connection fields looked like before this edit — an
        # identity change is diffed against it (see _retarget_active).
        previous = _flat_connection_snapshot(current)

        for key, value in changes.items():
            if key == "mqtt_password" and value == "••••••••":
                # The dialog echoes back a mask when the field was left alone.
                continue
            setattr(current, key, value)

        _coerce_flat(current)

        if current.connections:
            active_profile = next(
                (
                    p
                    for p in current.connections
                    if str(p.get("id")) == current.active_connection
                ),
                None,
            )
            if active_profile is None:
                raise ValueError(
                    f"Active connection `{current.active_connection}` is not in the list."
                )
            identity = _connection_identity(
                current.mqtt_broker_address,
                current.mqtt_port,
                current.mqtt_topic_prefix,
                current.mqtt_topic_suffix,
            )
            if identity != _profile_identity(active_profile):
                # A new server in the same dialog: another connection with a
                # clean database of its own (or the existing one for it).
                _retarget_active(current, previous)
            else:
                _absorb_flat(current, active_profile)

        self.save(current)
        return current

    # -- connection profiles ------------------------------------------------

    def _switch_to(self, settings: Settings, pid: str) -> None:
        """Point the flat working copy at the profile ``pid`` (no persist)."""
        ids = [str(p.get("id") or "") for p in settings.connections]
        if pid not in ids:
            raise ValueError(f"Unknown connection: {pid}")
        _apply_profile(settings, settings.connections[ids.index(pid)])
        settings.active_connection = pid

    def _reconcile(self, settings: Settings) -> None:
        """Bring the registry and the flat working copy in step (load path).

        An empty registry is a pre-connections config file: it becomes the
        first profile, keeping the existing database file as-is.  With a
        registry present the flat fields win — they are what the dialog
        writes — and an edited broker/topic follows the same rule as saving
        one: a new server becomes a new connection with its own database.
        """
        profiles = _normalize_profiles(settings.connections)
        if not profiles:
            pid = _derive_connection_id(
                settings.mqtt_broker_address, settings.subscribe_topic, []
            )
            name = _default_connection_name(
                settings.mqtt_broker_address,
                settings.mqtt_port,
                settings.subscribe_topic,
            )
            settings.connections = [
                _profile_from_flat(settings, pid, name, settings.db_file)
            ]
            settings.active_connection = pid
            settings.connection_name = name
            return

        settings.connections = profiles
        identity = _connection_identity(
            settings.mqtt_broker_address,
            settings.mqtt_port,
            settings.mqtt_topic_prefix,
            settings.mqtt_topic_suffix,
        )

        active = next(
            (
                p
                for p in profiles
                if str(p.get("id") or "") == str(settings.active_connection or "")
            ),
            None,
        )
        if active is None:
            # The active id vanished in a hand edit: identity decides who the
            # flat fields belong to, the first profile is the fallback.
            active = next(
                (p for p in profiles if _profile_identity(p) == identity),
                profiles[0],
            )
            settings.active_connection = str(active["id"])

        if identity != _profile_identity(active):
            _retarget_active(settings, _profile_snapshot(active))
        else:
            _absorb_flat(settings, active)

    def add_connection(self, **fields: Any) -> Settings:
        """Create a connection profile from these fields and make it active.

        Fields not provided fall back to the active profile's values, so a
        new broker inherits credentials/keys by default.  A profile with the
        same broker/port/topic already existing receives the edit instead of
        gaining a twin: one server, one database.
        """
        current = self.get()
        allowed = set(PROFILE_FIELDS) | {"connection_name"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(
                f"Unknown connection fields: {', '.join(sorted(unknown))}"
            )

        self._reconcile(current)  # registry present, flat == active profile
        previous = _flat_connection_snapshot(current)

        for key, value in fields.items():
            if key == "mqtt_password" and value == "••••••••":
                continue
            setattr(current, key, value)
        _coerce_flat(current)

        identity = _connection_identity(
            current.mqtt_broker_address,
            current.mqtt_port,
            current.mqtt_topic_prefix,
            current.mqtt_topic_suffix,
        )
        match = next(
            (p for p in current.connections if _profile_identity(p) == identity),
            None,
        )
        if match is None:
            taken = [str(p.get("id") or "") for p in current.connections]
            pid = _derive_connection_id(
                current.mqtt_broker_address, current.subscribe_topic, taken
            )
            name = str(fields.get("connection_name") or "").strip() or (
                _default_connection_name(
                    current.mqtt_broker_address,
                    current.mqtt_port,
                    current.subscribe_topic,
                )
            )
            profile = _profile_from_flat(
                current,
                pid,
                name,
                _derived_db_file(pid, str(previous.get("db_file") or "")),
            )
            current.connections.append(profile)
            current.active_connection = pid
            current.db_file = profile["db_file"]
            current.connection_name = name
        else:
            # Same server: the edit lands on the existing profile and only
            # really-changed fields travel over — its own credentials stay.
            for fname in PROFILE_FIELDS:
                if getattr(current, fname) != previous.get(fname):
                    match[fname] = getattr(current, fname)
            new_name = str(fields.get("connection_name") or "").strip()
            if new_name and new_name != str(match.get("name") or ""):
                match["name"] = new_name
            current.active_connection = str(match["id"])
            _apply_profile(current, match)

        self.save(current)
        return current

    def select_connection(self, pid: str) -> Settings:
        """Make an existing profile active; the flat fields follow it."""
        current = self.get()
        self._switch_to(current, str(pid))
        self.save(current)
        return current

    def remove_connection(self, pid: str) -> Settings:
        """Forget a profile; the active one falls back to the next remaining.

        Its database file is deliberately kept: deleting data is not
        reversible, and recreating the same server later re-attaches the
        very file (see _derived_db_file).
        """
        current = self.get()
        ids = [str(p.get("id") or "") for p in current.connections]
        if pid not in ids:
            raise ValueError(f"Unknown connection: {pid}")
        if len(ids) <= 1:
            raise ValueError("The last connection cannot be deleted.")
        current.connections = [
            p for p in current.connections if str(p.get("id") or "") != pid
        ]
        if pid == current.active_connection:
            remaining = [str(p.get("id") or "") for p in current.connections]
            self._switch_to(current, remaining[0])
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
