"""Settings: validation, persistence, password masking."""

from __future__ import annotations

import base64
import logging

import pytest
import yaml

from meshgraph.config import Settings, SettingsStore, validate


def test_default_settings_are_valid():
    assert validate(Settings()) == []


def test_rejects_empty_broker():
    errors = validate(Settings(mqtt_broker_address="   "))
    assert any("Broker address" in e for e in errors)


@pytest.mark.parametrize("port", [0, 70000, "abc"])
def test_rejects_bad_port(port):
    errors = validate(Settings(mqtt_port=port))
    assert any("port" in e.lower() for e in errors)


def test_rejects_bad_channel_key():
    errors = validate(Settings(decryption_keys="not-base64!!!"))
    assert any("base64" in e for e in errors)


def test_accepts_default_one_byte_key():
    """`AQ==` is the stock LongFast PSK (firmware expands it to defaultpsk)."""
    assert validate(Settings(decryption_keys="AQ==")) == []


def test_accepts_short_keys_that_firmware_pads():
    short = base64.b64encode(b"tooshort").decode()
    assert validate(Settings(decryption_keys=short)) == []


def test_rejects_oversized_channel_key():
    # nanopb caps ChannelSettings.psk at 32 bytes.
    big = base64.b64encode(b"x" * 33).decode()
    errors = validate(Settings(decryption_keys=big))
    assert any("at most 32 bytes" in e for e in errors)


def test_rejects_key_that_switches_encryption_off():
    errors = validate(Settings(decryption_keys="AA=="))  # a lone 0x00 byte
    assert any("encryption off" in e for e in errors)


def test_accepts_several_keys():
    keys = ",".join(
        [
            base64.b64encode(b"0" * 32).decode(),
            base64.b64encode(b"1" * 16).decode(),
        ]
    )
    assert validate(Settings(decryption_keys=keys)) == []


def test_accepts_combined_as_default_mode():
    assert validate(Settings(default_graph_mode="combined")) == []


def test_rejects_unknown_default_mode():
    errors = validate(Settings(default_graph_mode="nonsense"))
    assert any("Graph mode" in e for e in errors)


def test_rejects_subscription_without_wildcard():
    """`msh/RU/SAR` names one exact topic — nothing would ever match it."""
    errors = validate(Settings(mqtt_topic_suffix="/RU/SAR"))
    assert any("wildcard" in e for e in errors)


def test_accepts_wildcard_in_suffix():
    assert validate(Settings(mqtt_topic_suffix="/RU/SAR/#")) == []


def test_tls_flags_coerce_and_roundtrip(tmp_path):
    path = tmp_path / "config.yaml"
    store = SettingsStore(path=path)

    # A hand-written config.yaml may hold strings, the dialog JSON booleans.
    store.update(mqtt_port=8883, mqtt_tls=True, mqtt_tls_insecure="true")
    assert store.get().mqtt_tls is True
    assert store.get().mqtt_tls_insecure is True

    reloaded = SettingsStore(path=path).get()
    assert reloaded.mqtt_tls is True
    assert reloaded.mqtt_port == 8883

    store.update(mqtt_tls="false", mqtt_tls_insecure=False)
    assert store.get().mqtt_tls is False
    assert store.get().mqtt_tls_insecure is False


def test_save_and_reload_roundtrip(tmp_path):
    path = tmp_path / "config.yaml"
    store = SettingsStore(path=path)

    store.update(
        mqtt_broker_address="mqtt.example.org",
        mqtt_port=8883,
        mqtt_username="alice",
        mqtt_password="s3cret",
        default_graph_mode="rssi",
    )

    reloaded = SettingsStore(path=path).get()
    assert reloaded.mqtt_broker_address == "mqtt.example.org"
    assert reloaded.mqtt_port == 8883
    assert reloaded.mqtt_username == "alice"
    assert reloaded.mqtt_password == "s3cret"
    assert reloaded.default_graph_mode == "rssi"


def test_password_is_masked_in_api_view():
    masked = Settings(mqtt_password="hunter2").masked()
    assert masked["mqtt_password"] == "••••••••"
    assert masked["mqtt_password_set"] is True


def test_unset_password_reports_not_set():
    masked = Settings().masked()
    assert masked["mqtt_password_set"] is False


def test_masked_password_is_not_overwritten_on_update(settings_store):
    settings_store.update(mqtt_password="original")
    # The dialog echoes the mask back when the field was left untouched.
    updated = settings_store.update(mqtt_password="••••••••")
    assert updated.mqtt_password == "original"


def test_unknown_field_is_rejected(settings_store):
    with pytest.raises(ValueError, match="Unknown settings"):
        settings_store.update(broker_adress_typo="x")


def test_invalid_save_does_not_corrupt_file(settings_store):
    settings_store.update(mqtt_broker_address="ok.example")
    with pytest.raises(ValueError):
        settings_store.update(mqtt_port=99999)
    assert settings_store.get().mqtt_broker_address == "ok.example"
    assert settings_store.get().mqtt_port == 1883


def test_change_notification_fires(settings_store):
    seen = []
    settings_store.subscribe(lambda s: seen.append(s.mqtt_broker_address))
    settings_store.update(mqtt_broker_address="changed.example")
    assert seen == ["changed.example"]


def test_corrupt_yaml_falls_back_to_defaults(tmp_path, caplog):
    path = tmp_path / "config.yaml"
    path.write_text("{{ not yaml: [", encoding="utf-8")
    store = SettingsStore(path=path)
    with caplog.at_level(logging.WARNING, logger="meshgraph.config"):
        assert store.get().mqtt_port == 1883
    # Пользователь должен узнать, что его настройки проигнорированы (G-P2-4).
    assert str(path) in caplog.text


def test_non_mapping_yaml_is_also_logged(tmp_path, caplog):
    path = tmp_path / "config.yaml"
    path.write_text("- just\n- a list\n", encoding="utf-8")
    store = SettingsStore(path=path)
    with caplog.at_level(logging.WARNING, logger="meshgraph.config"):
        assert store.get().mqtt_port == 1883
    assert str(path) in caplog.text


def test_topic_and_keys_helpers():
    s = Settings(mqtt_topic_prefix="msh", decryption_keys="a, b ,,c")
    assert s.subscribe_topic == "msh/+/+/+/#"
    assert s.keys == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# Connection profiles: one server — one database file
# ---------------------------------------------------------------------------

def _profile_ids(settings: Settings) -> list[str]:
    return [str(p["id"]) for p in settings.connections]


def test_legacy_flat_config_becomes_the_first_profile(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "mqtt_broker_address: mqtt.old.example\n"
        "mqtt_port: 8883\n"
        "mqtt_topic_prefix: msh\n"
        "mqtt_topic_suffix: /RU/SAR/#\n"
        "db_file: data/legacy.db\n",
        encoding="utf-8",
    )
    settings = SettingsStore(path=path).get()
    assert len(settings.connections) == 1
    profile = settings.connections[0]
    assert settings.active_connection == profile["id"]
    # The existing database stays where it is — no data moves.
    assert profile["db_file"] == "data/legacy.db"
    assert settings.db_file == "data/legacy.db"
    assert settings.connection_name == profile["name"]


def test_credential_edit_keeps_profile_and_database(settings_store):
    settings_store.update(mqtt_username="alice", mqtt_password="pw")
    settings = settings_store.get()
    assert len(settings.connections) == 1
    assert settings.db_file == "data/graph.db"
    assert settings.connections[0]["mqtt_username"] == "alice"


def test_broker_change_spawns_a_new_connection_and_database(settings_store):
    settings_store.update(mqtt_broker_address="mqtt.other.example")
    settings = settings_store.get()
    assert len(settings.connections) == 2
    old = next(
        p for p in settings.connections if p["id"] != settings.active_connection
    )
    # The old profile keeps its file and stays fully intact.
    assert old["db_file"] == "data/graph.db"
    # The new one gets a file of its own next to it, named after the server.
    assert settings.db_file == "data/mqtt-other-example-msh.db"
    assert settings.db_file != old["db_file"]


def test_returning_to_the_old_server_reactivates_its_profile(settings_store):
    settings_store.update(mqtt_username="alice")
    settings_store.update(mqtt_broker_address="mqtt.other.example")
    settings_store.update(mqtt_broker_address="127.0.0.1")
    settings = settings_store.get()
    assert len(settings.connections) == 2  # no twins for servers seen before
    assert settings.db_file == "data/graph.db"
    assert settings.mqtt_username == "alice"  # its own credentials come back


def test_topic_prefix_change_also_splits(settings_store):
    settings_store.update(mqtt_topic_prefix="othernet")
    settings = settings_store.get()
    assert len(settings.connections) == 2
    assert settings.db_file == "data/127-0-0-1-othernet.db"


def test_port_change_splits_too(settings_store):
    settings_store.update(mqtt_port=8883, mqtt_tls=True)
    settings = settings_store.get()
    assert len(settings.connections) == 2
    # Same broker+topic → the id base collides → a suffix keeps it unique.
    assert settings.db_file == "data/127-0-0-1-msh-2.db"
    assert settings.mqtt_tls is True


def test_add_connection_creates_and_activates(settings_store):
    settings_store.add_connection(
        mqtt_broker_address="mesh.example", mqtt_topic_prefix="mesh"
    )
    settings = settings_store.get()
    assert settings.active_connection == "mesh-example-mesh"
    assert settings.db_file == "data/mesh-example-mesh.db"
    assert settings.connection_name == "mesh.example:1883 (mesh)"
    assert len(settings.connections) == 2


def test_add_connection_inherits_untouched_fields(settings_store):
    settings_store.update(mqtt_username="alice", decryption_keys="AQ==")
    settings_store.add_connection(mqtt_broker_address="mesh.example")
    settings = settings_store.get()
    assert settings.mqtt_username == "alice"  # credentials travel along
    assert settings.connections[0]["mqtt_username"] == "alice"  # old stays too


def test_add_connection_reuses_profile_with_same_identity(settings_store):
    settings_store.add_connection(mqtt_broker_address="mesh.example")
    settings = settings_store.add_connection(mqtt_broker_address="mesh.example")
    # Default profile + mesh.example: one server must not gain a twin.
    assert len(settings.connections) == 2
    assert settings.active_connection == "mesh-example-msh"


def test_add_connection_rejects_unknown_fields(settings_store):
    with pytest.raises(ValueError, match="Unknown connection fields"):
        settings_store.add_connection(broker_typo="x")


def test_select_connection_swaps_the_flat_fields(settings_store):
    settings_store.add_connection(
        mqtt_broker_address="mesh.example", mqtt_username="bob"
    )
    settings = settings_store.select_connection("127-0-0-1-msh")
    assert settings.mqtt_broker_address == "127.0.0.1"
    assert settings.db_file == "data/graph.db"
    assert settings.connection_name.startswith("127.0.0.1:1883")
    with pytest.raises(ValueError, match="Unknown connection"):
        settings_store.select_connection("nope")


def test_remove_connection_falls_back_and_keeps_the_file(settings_store, tmp_path):
    settings_store.update(db_file=str(tmp_path / "main.db"))
    created = settings_store.add_connection(mqtt_broker_address="mesh.example")
    db_path = tmp_path / "mesh-example-msh.db"
    assert created.db_file == str(db_path)
    db_path.write_bytes(b"not to be deleted")

    settings = settings_store.remove_connection(created.active_connection)
    assert len(settings.connections) == 1
    assert settings.active_connection == "127-0-0-1-msh"
    assert settings.db_file == str(tmp_path / "main.db")
    # Deleting a connection never destroys data.
    assert db_path.exists()


def test_remove_last_connection_is_refused(settings_store):
    active = settings_store.get().active_connection
    with pytest.raises(ValueError, match="last connection"):
        settings_store.remove_connection(active)


def test_masked_hides_every_profile_secret(settings_store):
    settings_store.add_connection(
        mqtt_broker_address="mesh.example", mqtt_password="s3cret"
    )
    masked = settings_store.get().masked()
    assert masked["connections"]
    for profile in masked["connections"]:
        assert "mqtt_password" not in profile
        assert "decryption_keys" not in profile
    # The active password keeps arriving in its usual masked form.
    assert masked["mqtt_password"] == "••••••••"
    assert "s3cret" not in str(masked["connections"])


def test_registry_survives_saves_and_reloads(settings_store):
    settings_store.update(mqtt_broker_address="a.example")
    settings_store.update(mqtt_username="bob")  # in-place on the new profile
    first = settings_store.get()
    reloaded = SettingsStore(path=settings_store.path).get()
    assert _profile_ids(reloaded) == _profile_ids(first)
    assert reloaded.active_connection == first.active_connection
    assert reloaded.db_file == first.db_file
    assert reloaded.mqtt_username == "bob"


def test_rename_updates_the_active_profile(settings_store):
    settings_store.update(connection_name="Мой сервер")
    settings = settings_store.get()
    assert settings.connection_name == "Мой сервер"
    assert settings.connections[0]["name"] == "Мой сервер"
    reloaded = SettingsStore(path=settings_store.path).get()
    assert reloaded.connection_name == "Мой сервер"


def test_hand_edited_broker_in_yaml_follows_the_new_server_rule(tmp_path):
    path = tmp_path / "config.yaml"
    store = SettingsStore(path=path)
    store.update(mqtt_username="alice")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["mqtt_broker_address"] = "hand.example"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    settings = SettingsStore(path=path).get()
    # The registry wins on the credentials, the flat identity spawns a new
    # connection with a database of its own.
    assert len(settings.connections) == 2
    assert settings.mqtt_broker_address == "hand.example"
    assert settings.mqtt_username == "alice"
    assert settings.db_file == "data/hand-example-msh.db"


def test_validate_rejects_shared_database_file():
    settings = Settings()
    settings.connections = [
        {
            "id": "a", "name": "A",
            "mqtt_broker_address": "a.example", "mqtt_port": 1883,
            "mqtt_topic_prefix": "msh", "db_file": "data/same.db",
        },
        {
            "id": "b", "name": "B",
            "mqtt_broker_address": "b.example", "mqtt_port": 1883,
            "mqtt_topic_prefix": "msh", "db_file": "data/same.db",
        },
    ]
    settings.active_connection = "a"
    errors = validate(settings)
    assert any("share the database file" in e for e in errors)


def test_validate_rejects_unknown_active_connection():
    settings = Settings()
    settings.connections = [
        {
            "id": "a", "name": "A",
            "mqtt_broker_address": "a.example", "mqtt_port": 1883,
            "mqtt_topic_prefix": "msh", "db_file": "data/a.db",
        },
    ]
    settings.active_connection = "ghost"
    errors = validate(settings)
    assert any("not in the list" in e for e in errors)


def test_validate_rejects_profile_without_broker():
    settings = Settings()
    settings.connections = [
        {"id": "a", "name": "A", "mqtt_broker_address": "  ",
         "mqtt_port": 1883, "db_file": "data/a.db"},
    ]
    settings.active_connection = "a"
    errors = validate(settings)
    assert any("needs a broker address" in e for e in errors)
