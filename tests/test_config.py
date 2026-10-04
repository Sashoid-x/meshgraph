"""Settings: validation, persistence, password masking."""

from __future__ import annotations

import base64

import pytest

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


def test_corrupt_yaml_falls_back_to_defaults(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("{{ not yaml: [", encoding="utf-8")
    store = SettingsStore(path=path)
    assert store.get().mqtt_port == 1883


def test_topic_and_keys_helpers():
    s = Settings(mqtt_topic_prefix="msh", decryption_keys="a, b ,,c")
    assert s.subscribe_topic == "msh/+/+/+/#"
    assert s.keys == ["a", "b", "c"]
