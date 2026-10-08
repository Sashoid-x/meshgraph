"""Settings: validation, persistence, password masking."""

from __future__ import annotations

import base64
import logging

import pytest
import yaml

from meshgraph.config import (
    Settings,
    SettingsStore,
    check_password,
    enabled_snapshots,
    hash_password,
    profile_settings,
    validate,
)


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


def test_topic_suffix_change_also_splits(settings_store):
    """The topic is prefix + suffix: another suffix is another data stream."""
    settings_store.update(mqtt_topic_suffix="/RU/BLK/#")
    settings = settings_store.get()
    assert len(settings.connections) == 2
    old = next(
        p for p in settings.connections if p["id"] != settings.active_connection
    )
    # The source connection keeps its own topic and its own file.
    assert old["mqtt_topic_suffix"] == "/+/+/+/#"
    assert old["db_file"] == "data/graph.db"
    # The copy's id and database come from the full subscribe topic.
    assert settings.db_file == "data/127-0-0-1-msh-ru-blk.db"
    assert settings.mqtt_topic_suffix == "/RU/BLK/#"


def test_add_connection_with_another_topic_copies_the_server(settings_store):
    """Reported case: same broker, only the topic differs → own connection."""
    settings = settings_store.add_connection(mqtt_topic_suffix="/RU/BLK/#")
    assert len(settings.connections) == 2
    # Broker, port, TLS and credentials are inherited from the active one.
    assert settings.mqtt_broker_address == "127.0.0.1"
    assert settings.mqtt_port == 1883
    assert settings.db_file == "data/127-0-0-1-msh-ru-blk.db"
    assert settings.connection_name == "127.0.0.1:1883 (msh/RU/BLK/#)"
    # The source connection is intact.
    source = next(p for p in settings.connections if p["id"] == "127-0-0-1-msh")
    assert source["mqtt_topic_suffix"] == "/+/+/+/#"
    assert source["db_file"] == "data/graph.db"

    # Saving the very same copy again does not create a twin.
    again = settings_store.add_connection(mqtt_topic_suffix="/RU/BLK/#")
    assert len(again.connections) == 2
    assert again.active_connection == "127-0-0-1-msh-ru-blk"


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
    # Default name carries the full subscribe topic — two topics on the same
    # broker must not look alike in the selector.
    assert settings.connection_name == "mesh.example:1883 (mesh/+/+/+/#)"
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


# ---------------------------------------------------------------------------
# Server manager: the enabled flag, snapshots, fallbacks
# ---------------------------------------------------------------------------


def test_profiles_default_to_enabled(settings_store):
    """Старый конфиг без ключа enabled читается как включённый сервер."""
    profiles = settings_store.get().connections
    assert profiles
    assert all(p["enabled"] is True for p in profiles)


def test_hand_edited_enabled_flag_is_coerced_on_load(tmp_path):
    """Ручная правка YAML: `off`/`on` превращаются в настоящие булевы."""
    from dataclasses import asdict

    path = tmp_path / "config.yaml"
    data = asdict(SettingsStore(path).get())  # дефолт с одним подключением

    data["connections"][0]["enabled"] = "off"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    assert SettingsStore(path).get().connections[0]["enabled"] is False

    data["connections"][0]["enabled"] = "on"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    assert SettingsStore(path).get().connections[0]["enabled"] is True


def test_enabled_snapshots_only_include_enabled_servers(settings_store):
    second = settings_store.add_connection(
        connection_name="Другой",
        mqtt_topic_prefix="other",
        mqtt_topic_suffix="/#",
    )
    first_id = [
        p["id"]
        for p in settings_store.get().connections
        if p["id"] != second.active_connection
    ][0]

    snapshots = enabled_snapshots(settings_store.get())
    assert set(snapshots) == {first_id, second.active_connection}

    settings_store.set_connection_enabled(second.active_connection, False)
    snapshots = enabled_snapshots(settings_store.get())
    assert set(snapshots) == {first_id}


def test_profile_settings_snapshot_is_isolated(settings_store):
    settings_store.update(retention_hours=48)
    second = settings_store.add_connection(
        connection_name="Другой",
        mqtt_topic_prefix="other",
        mqtt_topic_suffix="/#",
    )
    profile = settings_store.get_connection(second.active_connection)
    snapshot = profile_settings(settings_store.get(), profile)

    # Свойства сервера...
    assert snapshot.mqtt_topic_prefix == "other"
    assert snapshot.mqtt_topic_suffix == "/#"
    assert snapshot.db_file == profile["db_file"]
    assert snapshot.active_connection == profile["id"]
    # ...и общие настройки; правка снапшота не трогает хранилище.
    assert snapshot.retention_hours == 48
    snapshot.retention_hours = 7
    assert settings_store.get().retention_hours == 48


def test_disabling_the_active_connection_moves_the_view(settings_store):
    first_id = settings_store.get().active_connection
    second = settings_store.add_connection(
        connection_name="Второй", mqtt_topic_prefix="two", mqtt_topic_suffix="/#"
    )
    assert second.active_connection != first_id

    settings_store.set_connection_enabled(second.active_connection, False)

    current = settings_store.get()
    assert current.active_connection == first_id
    assert current.connections[0]["enabled"] is True


def test_disabling_the_last_enabled_keeps_the_view(settings_store):
    """Выключить всё можно: история активного читается, просто без сбора."""
    pid = settings_store.get().active_connection
    settings_store.set_connection_enabled(pid, False)

    current = settings_store.get()
    assert current.active_connection == pid
    assert current.connections[0]["enabled"] is False

    # Повторное выключение — бездумный no-op, не ошибка.
    assert settings_store.set_connection_enabled(pid, False).active_connection == pid


def test_unknown_connection_toggle_is_refused(settings_store):
    with pytest.raises(ValueError, match="Unknown connection"):
        settings_store.set_connection_enabled("ghost", False)


def test_remove_prefers_an_enabled_fallback(settings_store):
    settings_store.add_connection(connection_name="B", mqtt_topic_prefix="b")
    settings_store.add_connection(connection_name="C", mqtt_topic_prefix="c")
    ids = [p["id"] for p in settings_store.get().connections]
    settings_store.set_connection_enabled(ids[0], False)  # первый выключен

    settings_store.remove_connection(ids[2])  # удаляем активный C

    after = settings_store.get()
    assert after.active_connection == ids[1]  # включённый, а не первый подряд


def test_get_connection_returns_a_copy(settings_store):
    pid = settings_store.get().active_connection
    profile = settings_store.get_connection(pid)
    profile["name"] = "Хак"
    assert settings_store.get_connection(pid)["name"] != "Хак"
    assert settings_store.get_connection("ghost") is None


# ---------------------------------------------------------------------------
# Settings password (no login: one password guards the dialog)
# ---------------------------------------------------------------------------


def test_password_hash_roundtrip():
    encoded = hash_password("секрет")
    assert encoded.startswith("pbkdf2$")
    assert check_password("секрет", encoded)
    assert not check_password("другой", encoded)
    # Свежая соль на каждый вызов: две строки — разные.
    assert hash_password("секрет") != encoded
    # Пусто и битый формат никогда не совпадают.
    assert not check_password("", "")
    assert not check_password("x", "garbage")
    assert not check_password("x", "bcrypt$1$aa$bb")


def test_set_password_needs_no_old_one_then_requires_it(settings_store):
    settings_store.set_password("first-pass")
    assert check_password("first-pass", settings_store.get().settings_password_hash)

    with pytest.raises(ValueError, match="Current password"):
        settings_store.set_password("next", current_password="wrong")
    settings_store.set_password("next", current_password="first-pass")
    assert check_password("next", settings_store.get().settings_password_hash)

    settings_store.set_password("", current_password="next")  # снятие
    assert settings_store.get().settings_password_hash == ""


def test_settings_password_must_be_long_enough(settings_store):
    with pytest.raises(ValueError, match="at least 4"):
        settings_store.set_password("abc")


def test_masked_never_exposes_the_hash(settings_store):
    settings_store.set_password("secret")
    masked = settings_store.get().masked()
    assert "settings_password_hash" not in masked
    assert masked["settings_password_set"] is True


def test_update_refuses_raw_password_hash(settings_store):
    with pytest.raises(ValueError, match="set_password"):
        settings_store.update(settings_password_hash="pbkdf2$hacked")
